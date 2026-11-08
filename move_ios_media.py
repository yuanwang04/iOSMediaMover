#!/usr/bin/env python3
"""
iOS Media Mover
===============
A fast, safe utility and GUI application based on libimobiledevice to scan,
copy, verify, and move heavy media files (videos, photos, audio) from iOS
devices (iPhone / iPad) to Windows.

Key Optimizations & Features:
- Bundled libimobiledevice runtime (`bin/`): No MSYS2 required at runtime.
- Persistent in-process AFC connection via `ctypes` (`libimobiledevice-1.0.dll`),
  eliminating per-file process creation and repeated SSL handshakes (100x+ faster scan),
  with automatic fallback to `afcclient.exe` / `idevice_id.exe`.
- Full Tkinter/TTK Graphical User Interface with:
  * Auto-scanning Device selector (auto-selects when 1 device is connected)
  * Multi-choice Media Type filter by extension (defaults to all video formats)
  * File Size threshold input in MiB
  * Start Date & End Date calendar date pickers
  * Interactive file preview table, real-time transfer speed & progress bar
- Collision-safe local staging, size verification before deletion, and timestamp preservation.
"""

from __future__ import annotations

import argparse
import calendar
import ctypes
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

# --- Default Configuration (kept for CLI / script compatibility) ---

REMOTE_MEDIA_DIR = "DCIM"
LOCAL_TEMP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "temp_iphone_media")
FINAL_DESTINATION_DIR: Optional[str] = None

# Default threshold: 1 MiB
FILE_SIZE_THRESHOLD_MIB: float = 1.0
FILE_SIZE_THRESHOLD_BYTES: int = int(FILE_SIZE_THRESHOLD_MIB * 1024 * 1024)

# Media extensions grouped by category
VIDEO_EXTENSIONS: Tuple[str, ...] = (".MOV", ".MP4", ".M4V", ".3GP")
PHOTO_EXTENSIONS: Tuple[str, ...] = (".HEIC", ".JPG", ".JPEG", ".PNG", ".DNG", ".GIF", ".TIF", ".TIFF")
OTHER_EXTENSIONS: Tuple[str, ...] = (".AAE", ".M4A", ".WAV", ".MP3")

VIDEO_FILE_EXTENSION = ".MOV"  # Legacy single-extension constant

# Optional default date range strings ("YYYY-MM-DD" or None)
DATE_RANGE_START: Optional[str] = "2026-10-30"
DATE_RANGE_END: Optional[str] = None


# --- Binary & DLL Discovery ---

def get_bundle_base_dir() -> Path:
    """Returns the base directory for bundled resources (supports PyInstaller _MEIPASS)."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(getattr(sys, "_MEIPASS"))
    return Path(__file__).resolve().parent


def find_libimobiledevice_dir() -> Path:
    """
    Locates the directory containing libimobiledevice binaries and DLLs.
    Priority:
      1. Environment variable LIBIMOBILEDEVICE_PATH
      2. Bundled `<app_dir>/bin` directory (portable / packaged distribution)
      3. `C:/Program Files/libimobiledevice/mingw64/bin`
      4. System PATH lookup
    """
    candidates: List[Path] = []
    env_path = os.environ.get("LIBIMOBILEDEVICE_PATH")
    if env_path:
        candidates.append(Path(env_path))

    base_dir = get_bundle_base_dir()
    candidates.append(base_dir / "bin")
    candidates.append(Path(__file__).resolve().parent / "bin")
    candidates.append(Path("C:/Program Files/libimobiledevice/mingw64/bin"))

    for candidate in candidates:
        if candidate.is_dir() and (
            (candidate / "libimobiledevice-1.0.dll").exists()
            or (candidate / "afcclient.exe").exists()
        ):
            return candidate

    which_afc = shutil.which("afcclient.exe") or shutil.which("afcclient")
    if which_afc:
        return Path(which_afc).resolve().parent

    return base_dir / "bin"


LIBIMOBILEDEVICE_PATH = str(find_libimobiledevice_dir())
IDEVICE_ID_EXE = os.path.join(LIBIMOBILEDEVICE_PATH, "idevice_id.exe")
IDEVICENAME_EXE = os.path.join(LIBIMOBILEDEVICE_PATH, "idevicename.exe")
IDEVICEINFO_EXE = os.path.join(LIBIMOBILEDEVICE_PATH, "ideviceinfo.exe")
AFCCLIENT_EXE = os.path.join(LIBIMOBILEDEVICE_PATH, "afcclient.exe")


# --- Data Models ---

@dataclass
class DeviceInfo:
    udid: str
    name: str = ""
    model: str = ""
    product_version: str = ""
    free_bytes: Optional[int] = None
    total_bytes: Optional[int] = None

    @property
    def display_name(self) -> str:
        label = self.name.strip() if self.name else "iOS Device"
        if self.product_version:
            return f"{label} (iOS {self.product_version}) [{self.udid}]"
        return f"{label} [{self.udid}]"


@dataclass
class RemoteMediaFile:
    remote_path: str
    folder: str
    filename: str
    extension: str
    size: int
    mtime: float
    selected: bool = True

    @property
    def size_mib(self) -> float:
        return self.size / (1024 * 1024)

    @property
    def mtime_dt(self) -> datetime:
        return datetime.fromtimestamp(self.mtime)

    @property
    def mtime_str(self) -> str:
        try:
            return self.mtime_dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return "Unknown"


@dataclass
class ScanFilter:
    remote_root: str = REMOTE_MEDIA_DIR
    extensions: Set[str] = field(default_factory=lambda: set(VIDEO_EXTENSIONS))
    min_size_bytes: int = FILE_SIZE_THRESHOLD_BYTES
    start_ts: Optional[float] = None
    end_ts: Optional[float] = None

    def matches_extension(self, filename: str) -> bool:
        if not self.extensions:
            return True
        ext = os.path.splitext(filename)[1].upper()
        return ext in self.extensions


# --- Native ctypes Backend (Fast Persistent AFC Connection) ---

AFC_E_SUCCESS = 0
AFC_FOPEN_RDONLY = 0x00000001
IDEVICE_E_SUCCESS = 0
IDEVICE_E_NO_DEVICE = -3
LOCKDOWN_E_SUCCESS = 0


class NativeLibiMobileDevice:
    """
    Direct ctypes bindings to `libimobiledevice-1.0.dll`.
    Maintains a single persistent AFC connection for directory listing, file stats,
    high-speed streaming downloads, and file deletion without spawning subprocesses.
    """

    def __init__(self, bin_dir: Optional[str] = None):
        self.bin_dir = Path(bin_dir or find_libimobiledevice_dir())
        dll_path = self.bin_dir / "libimobiledevice-1.0.dll"
        if not dll_path.exists():
            raise FileNotFoundError(f"libimobiledevice-1.0.dll not found in {self.bin_dir}")

        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(str(self.bin_dir))
        os.environ["PATH"] = str(self.bin_dir) + os.pathsep + os.environ.get("PATH", "")

        self.dll = ctypes.CDLL(str(dll_path))
        self._setup_signatures()

    def _setup_signatures(self) -> None:
        c_void_p = ctypes.c_void_p
        c_char_p = ctypes.c_char_p
        c_int = ctypes.c_int
        c_uint32 = ctypes.c_uint32
        c_uint64 = ctypes.c_uint64
        POINTER = ctypes.POINTER

        # Device discovery & lifecycle
        self.dll.idevice_get_device_list.argtypes = [POINTER(POINTER(c_char_p)), POINTER(c_int)]
        self.dll.idevice_get_device_list.restype = c_int

        self.dll.idevice_device_list_free.argtypes = [POINTER(c_char_p)]
        self.dll.idevice_device_list_free.restype = c_int

        self.dll.idevice_new.argtypes = [POINTER(c_void_p), c_char_p]
        self.dll.idevice_new.restype = c_int

        self.dll.idevice_free.argtypes = [c_void_p]
        self.dll.idevice_free.restype = c_int

        # Lockdownd (for friendly device name)
        self.dll.lockdownd_client_new_with_handshake.argtypes = [c_void_p, POINTER(c_void_p), c_char_p]
        self.dll.lockdownd_client_new_with_handshake.restype = c_int

        self.dll.lockdownd_get_device_name.argtypes = [c_void_p, POINTER(c_void_p)]
        self.dll.lockdownd_get_device_name.restype = c_int

        self.dll.lockdownd_client_free.argtypes = [c_void_p]
        self.dll.lockdownd_client_free.restype = c_int

        # AFC service & operations
        self.dll.afc_client_start_service.argtypes = [c_void_p, POINTER(c_void_p), c_char_p]
        self.dll.afc_client_start_service.restype = c_int

        self.dll.afc_client_free.argtypes = [c_void_p]
        self.dll.afc_client_free.restype = c_int

        self.dll.afc_get_device_info.argtypes = [c_void_p, POINTER(POINTER(c_char_p))]
        self.dll.afc_get_device_info.restype = c_int

        self.dll.afc_read_directory.argtypes = [c_void_p, c_char_p, POINTER(POINTER(c_char_p))]
        self.dll.afc_read_directory.restype = c_int

        self.dll.afc_get_file_info.argtypes = [c_void_p, c_char_p, POINTER(POINTER(c_char_p))]
        self.dll.afc_get_file_info.restype = c_int

        self.dll.afc_dictionary_free.argtypes = [POINTER(c_char_p)]
        self.dll.afc_dictionary_free.restype = c_int

        self.dll.afc_file_open.argtypes = [c_void_p, c_char_p, c_int, POINTER(c_uint64)]
        self.dll.afc_file_open.restype = c_int

        self.dll.afc_file_read.argtypes = [c_void_p, c_uint64, c_char_p, c_uint32, POINTER(c_uint32)]
        self.dll.afc_file_read.restype = c_int

        self.dll.afc_file_close.argtypes = [c_void_p, c_uint64]
        self.dll.afc_file_close.restype = c_int

        self.dll.afc_remove_path.argtypes = [c_void_p, c_char_p]
        self.dll.afc_remove_path.restype = c_int

        self.dll.afc_strerror.argtypes = [c_int]
        self.dll.afc_strerror.restype = c_char_p

    def _read_string_array(self, ptr: ctypes.POINTER(ctypes.c_char_p)) -> List[str]:
        items: List[str] = []
        if not ptr:
            return items
        i = 0
        while True:
            val = ptr[i]
            if val is None or val == b"":
                break
            items.append(val.decode("utf-8", errors="replace"))
            i += 1
        return items

    def _read_kv_dict(self, ptr: ctypes.POINTER(ctypes.c_char_p)) -> Dict[str, str]:
        items = self._read_string_array(ptr)
        result: Dict[str, str] = {}
        for i in range(0, len(items) - 1, 2):
            result[items[i]] = items[i + 1]
        return result

    def list_devices(self) -> List[DeviceInfo]:
        devices_ptr = ctypes.POINTER(ctypes.c_char_p)()
        count = ctypes.c_int(0)
        res = self.dll.idevice_get_device_list(ctypes.byref(devices_ptr), ctypes.byref(count))
        if res == IDEVICE_E_NO_DEVICE or count.value <= 0:
            if devices_ptr:
                self.dll.idevice_device_list_free(devices_ptr)
            return []
        if res != IDEVICE_E_SUCCESS:
            if devices_ptr:
                self.dll.idevice_device_list_free(devices_ptr)
            raise RuntimeError(f"idevice_get_device_list failed with error code {res}")

        udids: List[str] = []
        try:
            for i in range(count.value):
                raw = devices_ptr[i]
                if raw:
                    udids.append(raw.decode("utf-8", errors="replace"))
        finally:
            if devices_ptr:
                self.dll.idevice_device_list_free(devices_ptr)

        # Deduplicate while preserving order
        seen: Set[str] = set()
        result: List[DeviceInfo] = []
        for udid in udids:
            if udid in seen:
                continue
            seen.add(udid)
            result.append(self.get_device_details(udid))
        return result

    def get_device_details(self, udid: str) -> DeviceInfo:
        info = DeviceInfo(udid=udid, name="iOS Device")
        dev = ctypes.c_void_p()
        if self.dll.idevice_new(ctypes.byref(dev), udid.encode("utf-8")) != IDEVICE_E_SUCCESS:
            return info

        try:
            # Query friendly device name via lockdownd
            lockdown = ctypes.c_void_p()
            if (
                self.dll.lockdownd_client_new_with_handshake(dev, ctypes.byref(lockdown), b"iOSMediaMover")
                == LOCKDOWN_E_SUCCESS
                and lockdown
            ):
                try:
                    name_ptr = ctypes.c_void_p()
                    if (
                        self.dll.lockdownd_get_device_name(lockdown, ctypes.byref(name_ptr)) == LOCKDOWN_E_SUCCESS
                        and name_ptr.value
                    ):
                        raw_name = ctypes.cast(name_ptr, ctypes.c_char_p).value
                        if raw_name:
                            info.name = raw_name.decode("utf-8", errors="replace")
                finally:
                    self.dll.lockdownd_client_free(lockdown)

            # Query AFC storage info (free/total space, model)
            afc = ctypes.c_void_p()
            if (
                self.dll.afc_client_start_service(dev, ctypes.byref(afc), b"iOSMediaMover") == AFC_E_SUCCESS
                and afc
            ):
                try:
                    info_ptr = ctypes.POINTER(ctypes.c_char_p)()
                    if self.dll.afc_get_device_info(afc, ctypes.byref(info_ptr)) == AFC_E_SUCCESS and info_ptr:
                        try:
                            kv = self._read_kv_dict(info_ptr)
                            info.model = kv.get("Model", "")
                            if "FSFreeBytes" in kv:
                                info.free_bytes = int(kv["FSFreeBytes"])
                            if "FSTotalBytes" in kv:
                                info.total_bytes = int(kv["FSTotalBytes"])
                        finally:
                            self.dll.afc_dictionary_free(info_ptr)
                finally:
                    self.dll.afc_client_free(afc)
        finally:
            self.dll.idevice_free(dev)

        return info

    def open_afc_session(self, udid: str) -> "NativeAFCSession":
        return NativeAFCSession(self, udid)


class NativeAFCSession:
    """Context manager holding an active `idevice_t` and `afc_client_t` connection."""

    def __init__(self, lib: NativeLibiMobileDevice, udid: str):
        self.lib = lib
        self.udid = udid
        self.dev = ctypes.c_void_p()
        self.afc = ctypes.c_void_p()

        res = self.lib.dll.idevice_new(ctypes.byref(self.dev), udid.encode("utf-8"))
        if res != IDEVICE_E_SUCCESS or not self.dev:
            raise RuntimeError(
                f"Could not connect to iOS device ({udid}). "
                "Please ensure the device is unlocked and trusted."
            )

        afc_res = self.lib.dll.afc_client_start_service(
            self.dev, ctypes.byref(self.afc), b"iOSMediaMover"
        )
        if afc_res != AFC_E_SUCCESS or not self.afc:
            self.lib.dll.idevice_free(self.dev)
            self.dev = ctypes.c_void_p()
            err_msg = self._err_str(afc_res)
            raise RuntimeError(
                f"Failed to start AFC file service on device ({err_msg}, code {afc_res}). "
                "Please unlock your iPhone/iPad and tap 'Trust' on the device screen."
            )

    def _err_str(self, code: int) -> str:
        raw = self.lib.dll.afc_strerror(code)
        return raw.decode("utf-8", errors="replace") if raw else f"AFC error {code}"

    def close(self) -> None:
        if self.afc:
            self.lib.dll.afc_client_free(self.afc)
            self.afc = ctypes.c_void_p()
        if self.dev:
            self.lib.dll.idevice_free(self.dev)
            self.dev = ctypes.c_void_p()

    def __enter__(self) -> "NativeAFCSession":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def list_dir(self, remote_path: str) -> List[str]:
        norm_path = "/" + remote_path.lstrip("/")
        entries_ptr = ctypes.POINTER(ctypes.c_char_p)()
        res = self.lib.dll.afc_read_directory(
            self.afc, norm_path.encode("utf-8"), ctypes.byref(entries_ptr)
        )
        if res != AFC_E_SUCCESS:
            if entries_ptr:
                self.lib.dll.afc_dictionary_free(entries_ptr)
            return []
        try:
            raw_entries = self.lib._read_string_array(entries_ptr)
            return [e for e in raw_entries if e not in (".", "..")]
        finally:
            if entries_ptr:
                self.lib.dll.afc_dictionary_free(entries_ptr)

    def get_file_info(self, remote_path: str) -> Optional[Dict[str, str]]:
        norm_path = "/" + remote_path.lstrip("/")
        info_ptr = ctypes.POINTER(ctypes.c_char_p)()
        res = self.lib.dll.afc_get_file_info(
            self.afc, norm_path.encode("utf-8"), ctypes.byref(info_ptr)
        )
        if res != AFC_E_SUCCESS or not info_ptr:
            if info_ptr:
                self.lib.dll.afc_dictionary_free(info_ptr)
            return None
        try:
            return self.lib._read_kv_dict(info_ptr)
        finally:
            self.lib.dll.afc_dictionary_free(info_ptr)

    def download_file(
        self,
        remote_path: str,
        local_path: str,
        expected_size: int = 0,
        mtime_sec: Optional[float] = None,
        progress_cb: Optional[Callable[[int, int], None]] = None,
        cancel_event: Optional[threading.Event] = None,
        chunk_size: int = 4 * 1024 * 1024,
    ) -> bool:
        norm_path = "/" + remote_path.lstrip("/")
        handle = ctypes.c_uint64(0)
        res = self.lib.dll.afc_file_open(
            self.afc, norm_path.encode("utf-8"), AFC_FOPEN_RDONLY, ctypes.byref(handle)
        )
        if res != AFC_E_SUCCESS:
            raise RuntimeError(f"Failed to open remote file '{remote_path}': {self._err_str(res)}")

        buf = ctypes.create_string_buffer(chunk_size)
        bytes_read = ctypes.c_uint32(0)
        total_written = 0
        os.makedirs(os.path.dirname(os.path.abspath(local_path)), exist_ok=True)

        try:
            with open(local_path, "wb") as out_f:
                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        raise InterruptedError("Transfer cancelled by user.")

                    read_res = self.lib.dll.afc_file_read(
                        self.afc, handle, buf, chunk_size, ctypes.byref(bytes_read)
                    )
                    if read_res != AFC_E_SUCCESS:
                        raise RuntimeError(
                            f"Error reading '{remote_path}': {self._err_str(read_res)}"
                        )
                    n = bytes_read.value
                    if n == 0:
                        break
                    out_f.write(memoryview(buf)[:n])
                    total_written += n
                    if progress_cb is not None:
                        progress_cb(total_written, expected_size)
        except Exception:
            # Clean up partial file on failure/cancel
            try:
                if os.path.exists(local_path):
                    os.remove(local_path)
            except OSError:
                pass
            raise
        finally:
            self.lib.dll.afc_file_close(self.afc, handle)

        if mtime_sec and mtime_sec > 0:
            try:
                os.utime(local_path, (mtime_sec, mtime_sec))
            except OSError:
                pass

        return os.path.exists(local_path) and (expected_size <= 0 or os.path.getsize(local_path) == expected_size)

    def remove_file(self, remote_path: str) -> None:
        norm_path = "/" + remote_path.lstrip("/")
        res = self.lib.dll.afc_remove_path(self.afc, norm_path.encode("utf-8"))
        if res != AFC_E_SUCCESS:
            raise RuntimeError(f"Failed to delete '{remote_path}': {self._err_str(res)}")


# --- Subprocess CLI Fallback Backend ---

def _subprocess_CreationFlags() -> int:
    if sys.platform == "win32":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return 0


def run_command(command: Sequence[str], check: bool = True) -> str:
    """Runs a libimobiledevice CLI command and returns stdout."""
    bin_dir = str(find_libimobiledevice_dir())
    env = os.environ.copy()
    env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")

    result = subprocess.run(
        list(command),
        capture_output=True,
        text=True,
        check=check,
        encoding="utf-8",
        errors="replace",
        env=env,
        creationflags=_subprocess_CreationFlags(),
    )
    return result.stdout.strip()


class CLIAFCSession:
    """Fallback session using `afcclient.exe` when ctypes DLL loading is unavailable."""

    def __init__(self, udid: str, bin_dir: Optional[str] = None):
        self.udid = udid
        self.bin_dir = Path(bin_dir or find_libimobiledevice_dir())
        self.afcclient_exe = str(self.bin_dir / "afcclient.exe")

    def __enter__(self) -> "CLIAFCSession":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        pass

    def close(self) -> None:
        pass

    def list_dir(self, remote_path: str) -> List[str]:
        out = run_command([self.afcclient_exe, "-u", self.udid, "ls", remote_path], check=False)
        if not out or out.startswith("Error:"):
            return []
        return [line.strip() for line in out.splitlines() if line.strip() and line.strip() not in (".", "..")]

    def get_file_info(self, remote_path: str) -> Optional[Dict[str, str]]:
        out = run_command([self.afcclient_exe, "-u", self.udid, "info", remote_path], check=False)
        if not out:
            return None
        try:
            parsed = json.loads(out)
            return {str(k): str(v) for k, v in parsed.items()}
        except json.JSONDecodeError:
            return None

    def download_file(
        self,
        remote_path: str,
        local_path: str,
        expected_size: int = 0,
        mtime_sec: Optional[float] = None,
        progress_cb: Optional[Callable[[int, int], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> bool:
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("Transfer cancelled by user.")
        os.makedirs(os.path.dirname(os.path.abspath(local_path)), exist_ok=True)
        run_command([self.afcclient_exe, "-u", self.udid, "get", "-f", remote_path, local_path], check=True)
        if progress_cb is not None and os.path.exists(local_path):
            progress_cb(os.path.getsize(local_path), expected_size)
        if mtime_sec and mtime_sec > 0 and os.path.exists(local_path):
            try:
                os.utime(local_path, (mtime_sec, mtime_sec))
            except OSError:
                pass
        return os.path.exists(local_path) and (expected_size <= 0 or os.path.getsize(local_path) == expected_size)

    def remove_file(self, remote_path: str) -> None:
        run_command([self.afcclient_exe, "-u", self.udid, "rm", remote_path], check=True)


# --- High-Level Media Mover Service ---

class MediaMoverService:
    """High-level service coordinating device discovery, scanning, copying, verification, and deletion."""

    def __init__(self, bin_dir: Optional[str] = None):
        self.bin_dir = str(bin_dir or find_libimobiledevice_dir())
        self.native: Optional[NativeLibiMobileDevice] = None
        try:
            self.native = NativeLibiMobileDevice(self.bin_dir)
        except Exception:
            self.native = None

    def list_devices(self) -> List[DeviceInfo]:
        """Returns a list of connected iOS devices with UDID and friendly name."""
        if self.native is not None:
            try:
                return self.native.list_devices()
            except Exception:
                pass

        # Fallback to idevice_id.exe
        idevice_id = os.path.join(self.bin_dir, "idevice_id.exe")
        idevicename = os.path.join(self.bin_dir, "idevicename.exe")
        if not os.path.exists(idevice_id):
            raise FileNotFoundError(
                f"Could not find libimobiledevice binaries in '{self.bin_dir}'."
            )
        out = run_command([idevice_id, "-l"], check=False)
        if not out:
            return []
        devices: List[DeviceInfo] = []
        seen: Set[str] = set()
        for line in out.splitlines():
            udid = line.strip()
            if not udid or udid in seen:
                continue
            seen.add(udid)
            name = "iOS Device"
            if os.path.exists(idevicename):
                name_out = run_command([idevicename, "-u", udid], check=False)
                if name_out and not name_out.startswith("ERROR"):
                    name = name_out.strip()
            devices.append(DeviceInfo(udid=udid, name=name))
        return devices

    def open_session(self, udid: str):
        if self.native is not None:
            return self.native.open_afc_session(udid)
        return CLIAFCSession(udid, self.bin_dir)

    def scan_media(
        self,
        udid: str,
        scan_filter: ScanFilter,
        log_cb: Optional[Callable[[str], None]] = None,
        progress_cb: Optional[Callable[[str, int, int], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> List[RemoteMediaFile]:
        """
        Scans `DCIM` (and its subdirectories) over a single persistent AFC session.
        """
        def log(msg: str) -> None:
            if log_cb:
                log_cb(msg)

        matched_files: List[RemoteMediaFile] = []
        root_dir = scan_filter.remote_root.strip("/") or "DCIM"

        with self.open_session(udid) as session:
            sub_dirs = session.list_dir(root_dir)
            if not sub_dirs:
                log(f"No subdirectories found in /{root_dir}.")
                return []

            total_dirs = len(sub_dirs)
            log(f"Scanning {total_dirs} folder(s) inside /{root_dir}...")

            for idx, sub_dir in enumerate(sub_dirs, start=1):
                if cancel_event is not None and cancel_event.is_set():
                    log("Scan cancelled by user.")
                    break

                remote_dir_path = f"{root_dir}/{sub_dir}"
                if progress_cb:
                    progress_cb(f"Scanning {remote_dir_path} ({idx}/{total_dirs})...", idx - 1, total_dirs)

                dir_info = session.get_file_info(remote_dir_path)
                if not dir_info or dir_info.get("st_ifmt") != "S_IFDIR":
                    continue

                entries = session.list_dir(remote_dir_path)
                if not entries:
                    continue

                # Pre-filter by extension before querying individual file stats!
                # This avoids stat calls on non-matching files (e.g., skipping .HEIC/.JPG when scanning videos).
                candidate_names = [
                    name for name in entries if scan_filter.matches_extension(name)
                ]
                if not candidate_names:
                    continue

                log(f"  [{sub_dir}] Checking {len(candidate_names)} matching media file(s)...")

                for filename in candidate_names:
                    if cancel_event is not None and cancel_event.is_set():
                        break

                    remote_file_path = f"{remote_dir_path}/{filename}"
                    f_info = session.get_file_info(remote_file_path)
                    if not f_info or f_info.get("st_ifmt") != "S_IFREG":
                        continue

                    file_size = int(f_info.get("st_size", 0))
                    raw_mtime = int(f_info.get("st_mtime", 0))
                    # libimobiledevice AFC reports timestamps in nanoseconds since epoch
                    file_ts = raw_mtime / 1_000_000_000.0 if raw_mtime > 1e12 else float(raw_mtime)

                    if scan_filter.min_size_bytes > 0 and file_size < scan_filter.min_size_bytes:
                        continue
                    if scan_filter.start_ts is not None and file_ts < scan_filter.start_ts:
                        continue
                    if scan_filter.end_ts is not None and file_ts > scan_filter.end_ts:
                        continue

                    ext = os.path.splitext(filename)[1].upper()
                    media_file = RemoteMediaFile(
                        remote_path=remote_file_path,
                        folder=sub_dir,
                        filename=filename,
                        extension=ext,
                        size=file_size,
                        mtime=file_ts,
                        selected=True,
                    )
                    matched_files.append(media_file)

            if progress_cb:
                progress_cb("Scan complete.", total_dirs, total_dirs)

        # Sort newest first by default
        matched_files.sort(key=lambda f: (f.mtime, f.remote_path), reverse=True)
        return matched_files

    @staticmethod
    def resolve_local_filenames(files: Sequence[RemoteMediaFile]) -> Dict[str, str]:
        """
        Maps each `remote_path` to a collision-free local filename.
        If two folders have `IMG_0001.MOV` (e.g. `100APPLE/IMG_0001.MOV` and `101APPLE/IMG_0001.MOV`),
        prefixes the duplicate with its folder name (`101APPLE_IMG_0001.MOV`).
        """
        counts: Dict[str, int] = {}
        for f in files:
            key = f.filename.upper()
            counts[key] = counts.get(key, 0) + 1

        mapping: Dict[str, str] = {}
        used: Set[str] = set()
        for f in files:
            if counts[f.filename.upper()] > 1:
                candidate = f"{f.folder}_{f.filename}"
            else:
                candidate = f.filename

            base, ext = os.path.splitext(candidate)
            counter = 1
            while candidate.upper() in used:
                candidate = f"{base}_{counter}{ext}"
                counter += 1
            used.add(candidate.upper())
            mapping[f.remote_path] = candidate
        return mapping

    def copy_and_verify_files(
        self,
        udid: str,
        files_to_copy: Sequence[RemoteMediaFile],
        staging_dir: str,
        log_cb: Optional[Callable[[str], None]] = None,
        progress_cb: Optional[Callable[[int, int, int, int, float, str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> Tuple[bool, Dict[str, str]]:
        """
        Copies selected files from the iOS device to `staging_dir` over a single AFC connection
        and verifies every file's size on disk.

        Returns `(all_verified, remote_to_local_path_map)`.
        """
        def log(msg: str) -> None:
            if log_cb:
                log_cb(msg)

        if not files_to_copy:
            return True, {}

        os.makedirs(staging_dir, exist_ok=True)
        name_map = self.resolve_local_filenames(files_to_copy)
        local_paths: Dict[str, str] = {}

        total_files = len(files_to_copy)
        total_bytes = sum(f.size for f in files_to_copy)
        completed_bytes_prior = 0
        start_time = time.perf_counter()

        log(f"Copying {total_files} file(s) ({total_bytes / (1024 ** 2):.2f} MiB) to '{staging_dir}'...")

        with self.open_session(udid) as session:
            for idx, item in enumerate(files_to_copy, start=1):
                if cancel_event is not None and cancel_event.is_set():
                    log("Transfer cancelled by user.")
                    return False, local_paths

                local_filename = name_map[item.remote_path]
                local_path = os.path.join(staging_dir, local_filename)
                local_paths[item.remote_path] = local_path

                # Skip re-downloading if already present with exact size
                if os.path.exists(local_path) and os.path.getsize(local_path) == item.size:
                    log(f"  [{idx}/{total_files}] Skipping {local_filename} (already downloaded & verified)")
                    completed_bytes_prior += item.size
                    elapsed = max(time.perf_counter() - start_time, 0.001)
                    speed_mibs = (completed_bytes_prior / (1024 * 1024)) / elapsed
                    if progress_cb:
                        progress_cb(idx, total_files, completed_bytes_prior, total_bytes, speed_mibs, local_filename)
                    continue

                log(f"  [{idx}/{total_files}] Copying {item.remote_path} -> {local_filename} ({item.size_mib:.2f} MiB)...")

                def on_chunk(file_bytes_done: int, _file_total: int, base_done: int = completed_bytes_prior, fname: str = local_filename, f_idx: int = idx) -> None:
                    overall_done = base_done + file_bytes_done
                    elapsed = max(time.perf_counter() - start_time, 0.001)
                    speed_mibs = (overall_done / (1024 * 1024)) / elapsed
                    if progress_cb:
                        progress_cb(f_idx, total_files, overall_done, total_bytes, speed_mibs, fname)

                ok = session.download_file(
                    remote_path=item.remote_path,
                    local_path=local_path,
                    expected_size=item.size,
                    mtime_sec=item.mtime,
                    progress_cb=on_chunk,
                    cancel_event=cancel_event,
                )
                if not ok:
                    log(f"  [FAIL] Copy failed for {item.remote_path}")
                    return False, local_paths

                completed_bytes_prior += item.size

        # Verification step
        log("Verifying copied files on disk...")
        all_verified = True
        for item in files_to_copy:
            local_path = local_paths.get(item.remote_path, "")
            if os.path.exists(local_path) and os.path.getsize(local_path) == item.size:
                log(f"  [OK] Verified {os.path.basename(local_path)} ({item.size_mib:.2f} MiB)")
            else:
                log(f"  [FAIL] Size mismatch or missing file for {item.remote_path}!")
                all_verified = False

        return all_verified, local_paths

    def delete_files_from_device(
        self,
        udid: str,
        files_to_delete: Sequence[RemoteMediaFile],
        log_cb: Optional[Callable[[str], None]] = None,
    ) -> int:
        """Deletes verified files from the iOS device over a single AFC connection."""
        def log(msg: str) -> None:
            if log_cb:
                log_cb(msg)

        if not files_to_delete:
            return 0

        deleted_count = 0
        log(f"Deleting {len(files_to_delete)} file(s) from iOS device...")
        with self.open_session(udid) as session:
            for item in files_to_delete:
                log(f"  Deleting {item.remote_path}...")
                session.remove_file(item.remote_path)
                deleted_count += 1
        log(f"Deleted {deleted_count} file(s) from iOS device.")
        return deleted_count

    @staticmethod
    def move_staged_to_destination(
        local_paths: Dict[str, str],
        staging_dir: str,
        final_destination_dir: Optional[str],
        log_cb: Optional[Callable[[str], None]] = None,
    ) -> str:
        """Moves verified files from `staging_dir` to `final_destination_dir` if specified."""
        def log(msg: str) -> None:
            if log_cb:
                log_cb(msg)

        if not final_destination_dir or os.path.abspath(final_destination_dir) == os.path.abspath(staging_dir):
            log(f"Files are stored in: {staging_dir}")
            return staging_dir

        os.makedirs(final_destination_dir, exist_ok=True)
        log(f"Moving verified files to final destination: {final_destination_dir}")

        for remote_path, src_path in local_paths.items():
            if not os.path.exists(src_path):
                continue
            filename = os.path.basename(src_path)
            dest_path = os.path.join(final_destination_dir, filename)
            if os.path.exists(dest_path):
                # Avoid overwriting a different file in destination
                if os.path.getsize(dest_path) == os.path.getsize(src_path):
                    log(f"  File {filename} already exists in destination with matching size; removing temp copy.")
                    os.remove(src_path)
                    continue
                base, ext = os.path.splitext(filename)
                counter = 1
                while os.path.exists(dest_path):
                    dest_path = os.path.join(final_destination_dir, f"{base}_{counter}{ext}")
                    counter += 1
            shutil.move(src_path, dest_path)

        try:
            if os.path.isdir(staging_dir) and not os.listdir(staging_dir):
                os.rmdir(staging_dir)
        except OSError:
            pass

        return final_destination_dir


# --- Date Parsing Helpers ---

def parse_date_bounds(
    start_date_str: Optional[str],
    end_date_str: Optional[str],
) -> Tuple[Optional[float], Optional[float]]:
    """
    Parses YYYY-MM-DD strings into start-of-day and end-of-day (23:59:59.999999) timestamps.
    """
    start_ts: Optional[float] = None
    end_ts: Optional[float] = None

    if start_date_str and start_date_str.strip():
        d_start = datetime.strptime(start_date_str.strip(), "%Y-%m-%d").date()
        start_ts = datetime.combine(d_start, dt_time.min).timestamp()

    if end_date_str and end_date_str.strip():
        d_end = datetime.strptime(end_date_str.strip(), "%Y-%m-%d").date()
        end_ts = datetime.combine(d_end, dt_time.max).timestamp()

    if start_ts is not None and end_ts is not None and start_ts > end_ts:
        raise ValueError("Start Date cannot be later than End Date.")

    return start_ts, end_ts


# --- Legacy CLI Functions (Preserved for compatibility) ---

def get_device_udid() -> str:
    """Gets the UDID of the first connected iOS device."""
    service = MediaMoverService()
    devices = service.list_devices()
    if not devices:
        print("Error: No iOS device found. Please connect your device via USB and trust this computer.")
        sys.exit(1)
    if len(devices) > 1:
        print(f"Warning: Multiple devices found. Using the first one: {devices[0].display_name}")
    print(f"Found device: {devices[0].display_name}")
    return devices[0].udid


def run_cli_mode(args: argparse.Namespace) -> None:
    """Runs the media mover in command-line mode."""
    service = MediaMoverService()
    devices = service.list_devices()
    if not devices:
        print("Error: No iOS device found. Please connect your device via USB.")
        sys.exit(1)

    selected_device = devices[0]
    if args.udid:
        matches = [d for d in devices if d.udid == args.udid]
        if not matches:
            print(f"Error: Device with UDID '{args.udid}' not found.")
            sys.exit(1)
        selected_device = matches[0]
    elif len(devices) > 1:
        print(f"Note: Multiple devices found; defaulting to {selected_device.display_name}")

    exts = {
        (e if e.startswith(".") else f".{e}").upper()
        for e in (args.extensions or list(VIDEO_EXTENSIONS))
    }
    min_bytes = int(float(args.min_size_mib) * 1024 * 1024)
    start_ts, end_ts = parse_date_bounds(args.start_date, args.end_date)

    scan_filter = ScanFilter(
        remote_root=REMOTE_MEDIA_DIR,
        extensions=exts,
        min_size_bytes=min_bytes,
        start_ts=start_ts,
        end_ts=end_ts,
    )

    print(f"--> Connected to: {selected_device.display_name}")
    print(f"--> Scanning for {', '.join(sorted(exts))} >= {args.min_size_mib:.2f} MiB...")
    files_to_move = service.scan_media(
        selected_device.udid,
        scan_filter=scan_filter,
        log_cb=print,
    )

    if not files_to_move:
        print("\nNo matching media files found. Exiting.")
        return

    total_mib = sum(f.size_mib for f in files_to_move)
    print(f"\nFound {len(files_to_move)} file(s), totaling {total_mib:.2f} MiB ({total_mib / 1024:.2f} GiB).")

    dest_dir = args.destination if args.destination is not None else FINAL_DESTINATION_DIR
    verified, local_paths = service.copy_and_verify_files(
        selected_device.udid,
        files_to_move,
        staging_dir=LOCAL_TEMP_DIR,
        log_cb=print,
    )
    if not verified:
        print("\nVerification of copied files failed. Aborting before deletion.")
        sys.exit(1)

    print("\nAll files copied and verified successfully.")
    if args.copy_only:
        service.move_staged_to_destination(local_paths, LOCAL_TEMP_DIR, dest_dir, log_cb=print)
        print("\nCopy complete (original files kept on device).")
        return

    while True:
        confirm = input("Do you want to delete the original files from the iPhone? (yes/no): ").strip().lower()
        if confirm in ("yes", "no"):
            break
        print("Invalid input. Please enter 'yes' or 'no'.")

    if confirm == "yes":
        service.delete_files_from_device(selected_device.udid, files_to_move, log_cb=print)
        service.move_staged_to_destination(local_paths, LOCAL_TEMP_DIR, dest_dir, log_cb=print)
        print("\nProcess complete!")
    else:
        print("\nAborted deletion. Original files were kept on the device.")
        service.move_staged_to_destination(local_paths, LOCAL_TEMP_DIR, dest_dir, log_cb=print)


# --- Graphical User Interface (Tkinter / TTK) ---

def launch_gui() -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    # Enable high-DPI crisp rendering on Windows
    if sys.platform == "win32":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass

    class CalendarPopup(tk.Toplevel):
        """Lightweight, zero-dependency interactive calendar date chooser popup."""

        def __init__(self, parent: tk.Widget, initial_date: Optional[date], on_select: Callable[[date], None]):
            super().__init__(parent)
            self.withdraw()
            self.title("Choose Date")
            self.resizable(False, False)
            self.transient(parent.winfo_toplevel())
            self.grab_set()

            self.on_select = on_select
            today = date.today()
            init = initial_date or today
            self.year = init.year
            self.month = init.month
            self.selected_date = init

            self.configure(bg="#F8FAFC", padx=10, pady=10)

            # Header (Month / Year navigation)
            hdr = ttk.Frame(self)
            hdr.pack(fill="x", pady=(0, 6))

            ttk.Button(hdr, text="«", width=3, command=self._prev_year).pack(side="left", padx=1)
            ttk.Button(hdr, text="‹", width=3, command=self._prev_month).pack(side="left", padx=1)

            self.title_lbl = ttk.Label(hdr, text="", font=("Segoe UI", 10, "bold"), anchor="center")
            self.title_lbl.pack(side="left", fill="x", expand=True, padx=6)

            ttk.Button(hdr, text="›", width=3, command=self._next_month).pack(side="right", padx=1)
            ttk.Button(hdr, text="»", width=3, command=self._next_year).pack(side="right", padx=1)

            # Calendar grid frame
            self.grid_frame = ttk.Frame(self)
            self.grid_frame.pack(fill="both", expand=True)

            # Footer buttons
            ftr = ttk.Frame(self)
            ftr.pack(fill="x", pady=(8, 0))
            ttk.Button(ftr, text="Today", command=self._pick_today).pack(side="left")
            ttk.Button(ftr, text="Cancel", command=self.destroy).pack(side="right")

            self._render_days()

            # Position near parent widget
            self.update_idletasks()
            x = parent.winfo_rootx()
            y = parent.winfo_rooty() + parent.winfo_height() + 4
            self.geometry(f"+{x}+{y}")
            self.deiconify()

        def _prev_month(self) -> None:
            if self.month == 1:
                self.month = 12
                self.year -= 1
            else:
                self.month -= 1
            self._render_days()

        def _next_month(self) -> None:
            if self.month == 12:
                self.month = 1
                self.year += 1
            else:
                self.month += 1
            self._render_days()

        def _prev_year(self) -> None:
            self.year -= 1
            self._render_days()

        def _next_year(self) -> None:
            self.year += 1
            self._render_days()

        def _pick_today(self) -> None:
            self.on_select(date.today())
            self.destroy()

        def _pick_day(self, day_num: int) -> None:
            chosen = date(self.year, self.month, day_num)
            self.on_select(chosen)
            self.destroy()

        def _render_days(self) -> None:
            for child in self.grid_frame.winfo_children():
                child.destroy()

            self.title_lbl.configure(text=f"{calendar.month_name[self.month]} {self.year}")

            days_header = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
            for col, d_name in enumerate(days_header):
                lbl = ttk.Label(
                    self.grid_frame,
                    text=d_name,
                    font=("Segoe UI", 9, "bold"),
                    anchor="center",
                    width=4,
                )
                lbl.grid(row=0, column=col, padx=1, pady=2)

            cal = calendar.Calendar(firstweekday=0)
            month_weeks = cal.monthdayscalendar(self.year, self.month)
            today = date.today()

            for r_idx, week in enumerate(month_weeks, start=1):
                for c_idx, day_num in enumerate(week):
                    if day_num == 0:
                        ttk.Label(self.grid_frame, text="", width=4).grid(row=r_idx, column=c_idx, padx=1, pady=1)
                        continue

                    cur_date = date(self.year, self.month, day_num)
                    is_selected = cur_date == self.selected_date
                    is_today = cur_date == today

                    btn = tk.Button(
                        self.grid_frame,
                        text=str(day_num),
                        width=3,
                        relief="flat",
                        font=("Segoe UI", 9, "bold" if (is_selected or is_today) else "normal"),
                        bg="#2563EB" if is_selected else ("#DBEAFE" if is_today else "#FFFFFF"),
                        fg="#FFFFFF" if is_selected else ("#1D4ED8" if is_today else "#1E293B"),
                        activebackground="#1D4ED8",
                        activeforeground="#FFFFFF",
                        cursor="hand2",
                        command=lambda d=day_num: self._pick_day(d),
                    )
                    btn.grid(row=r_idx, column=c_idx, padx=1, pady=1)

    class DatePickerField(ttk.Frame):
        """Composite widget: checkbox enable toggle + YYYY-MM-DD entry + calendar popup button."""

        def __init__(
            self,
            parent: tk.Widget,
            label_text: str,
            default_enabled: bool = False,
            default_date: Optional[str] = None,
        ):
            super().__init__(parent)
            self.enabled_var = tk.BooleanVar(value=default_enabled)
            self.date_var = tk.StringVar(value=default_date or date.today().strftime("%Y-%m-%d"))

            self.chk = ttk.Checkbutton(
                self,
                text=label_text,
                variable=self.enabled_var,
                command=self._sync_state,
                width=11,
            )
            self.chk.pack(side="left", padx=(0, 4))

            self.entry = ttk.Entry(self, textvariable=self.date_var, width=13, font=("Segoe UI", 9))
            self.entry.pack(side="left", padx=(0, 4))

            self.cal_btn = ttk.Button(self, text="📅 Pick...", width=9, command=self._open_calendar)
            self.cal_btn.pack(side="left")

            self._sync_state()

        def _sync_state(self) -> None:
            if self.enabled_var.get():
                self.entry.configure(state="normal")
                self.cal_btn.configure(state="normal")
            else:
                self.entry.configure(state="disabled")
                self.cal_btn.configure(state="disabled")

        def _open_calendar(self) -> None:
            current: Optional[date] = None
            try:
                current = datetime.strptime(self.date_var.get().strip(), "%Y-%m-%d").date()
            except ValueError:
                current = date.today()

            def on_picked(chosen: date) -> None:
                self.date_var.set(chosen.strftime("%Y-%m-%d"))
                self.enabled_var.set(True)
                self._sync_state()

            CalendarPopup(self.cal_btn, current, on_picked)

        def get_date_str(self) -> Optional[str]:
            if not self.enabled_var.get():
                return None
            val = self.date_var.get().strip()
            return val if val else None

    class IOSMediaMoverApp:
        """Main Tkinter Application for iOS Media Mover."""

        def __init__(self, root: tk.Tk):
            self.root = root
            self.root.title("iOS Media Mover")
            self.root.geometry("980x740")
            self.root.minsize(860, 640)

            self.service = MediaMoverService()
            self.devices: List[DeviceInfo] = []
            self.scanned_files: List[RemoteMediaFile] = []
            self.cancel_event = threading.Event()
            self.is_busy = False
            self.ui_queue: "queue.Queue[Callable[[], None]]" = queue.Queue()

            self._configure_styles()
            self._build_ui()

            # Start UI queue poller
            self.root.after(50, self._process_ui_queue)
            # Auto-scan devices on startup
            self.root.after(150, self.scan_devices)

        def _configure_styles(self) -> None:
            style = ttk.Style(self.root)
            if "vista" in style.theme_names():
                style.theme_use("vista")
            elif "clam" in style.theme_names():
                style.theme_use("clam")

            style.configure("Header.TLabel", font=("Segoe UI", 14, "bold"))
            style.configure("SubHeader.TLabel", font=("Segoe UI", 9), foreground="#475569")
            style.configure("Section.TLabelframe.Label", font=("Segoe UI", 10, "bold"))
            style.configure("Accent.TButton", font=("Segoe UI", 9, "bold"), padding=(10, 5))
            style.configure("Treeview", rowheight=24, font=("Segoe UI", 9))
            style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))

        def _build_ui(self) -> None:
            outer = ttk.Frame(self.root, padding=12)
            outer.pack(fill="both", expand=True)

            # Top Title Bar
            top_bar = ttk.Frame(outer)
            top_bar.pack(fill="x", pady=(0, 8))
            ttk.Label(top_bar, text="📱 iOS Media Mover", style="Header.TLabel").pack(side="left")
            backend_name = (
                "Native AFC DLL (Fast Persistent Session)"
                if self.service.native is not None
                else "CLI Subprocess Fallback"
            )
            ttk.Label(
                top_bar,
                text=f"Engine: {backend_name}",
                style="SubHeader.TLabel",
            ).pack(side="right", pady=(4, 0))

            # 1. Device Selection Frame
            dev_frame = ttk.LabelFrame(outer, text=" 1. iOS Device ", style="Section.TLabelframe", padding=10)
            dev_frame.pack(fill="x", pady=(0, 8))

            dev_row = ttk.Frame(dev_frame)
            dev_row.pack(fill="x")

            ttk.Label(dev_row, text="Device:", width=10).pack(side="left")
            self.device_var = tk.StringVar()
            self.device_combo = ttk.Combobox(
                dev_row,
                textvariable=self.device_var,
                state="readonly",
                font=("Segoe UI", 9),
            )
            self.device_combo.pack(side="left", fill="x", expand=True, padx=(0, 8))
            self.device_combo.bind("<<ComboboxSelected>>", self._on_device_selected)

            self.scan_dev_btn = ttk.Button(dev_row, text="🔄 Scan Devices", command=self.scan_devices)
            self.scan_dev_btn.pack(side="left")

            self.device_status_var = tk.StringVar(value="Scanning for connected iOS devices...")
            ttk.Label(dev_frame, textvariable=self.device_status_var, style="SubHeader.TLabel").pack(
                fill="x", pady=(6, 0)
            )

            # 2. Filters Frame (Media Type, File Size, Start Date & End Date)
            filter_frame = ttk.LabelFrame(
                outer, text=" 2. Filter Options ", style="Section.TLabelframe", padding=10
            )
            filter_frame.pack(fill="x", pady=(0, 8))

            # 2a. Media Type (Multi-choice by extension, default to all videos)
            ext_label_row = ttk.Frame(filter_frame)
            ext_label_row.pack(fill="x", pady=(0, 4))
            ttk.Label(ext_label_row, text="Media Type (Extensions):", font=("Segoe UI", 9, "bold")).pack(side="left")

            # Quick preset buttons for extensions
            ttk.Button(ext_label_row, text="Videos Only (Default)", command=self._select_videos_only).pack(
                side="right", padx=(4, 0)
            )
            ttk.Button(ext_label_row, text="Photos Only", command=self._select_photos_only).pack(
                side="right", padx=(4, 0)
            )
            ttk.Button(ext_label_row, text="Select All", command=self._select_all_extensions).pack(
                side="right", padx=(4, 0)
            )
            ttk.Button(ext_label_row, text="Clear", command=self._clear_all_extensions).pack(
                side="right", padx=(4, 0)
            )

            self.ext_vars: Dict[str, tk.BooleanVar] = {}
            ext_grid = ttk.Frame(filter_frame)
            ext_grid.pack(fill="x", pady=(2, 6))

            # Video row (Default checked)
            ttk.Label(ext_grid, text="Videos:", width=10, foreground="#1E40AF").grid(
                row=0, column=0, sticky="w", pady=2
            )
            for idx, ext in enumerate(VIDEO_EXTENSIONS, start=1):
                var = tk.BooleanVar(value=True)  # Default to all videos checked
                self.ext_vars[ext] = var
                ttk.Checkbutton(ext_grid, text=ext, variable=var).grid(
                    row=0, column=idx, sticky="w", padx=(0, 12), pady=2
                )

            # Photos row (Default unchecked)
            ttk.Label(ext_grid, text="Photos:", width=10, foreground="#047857").grid(
                row=1, column=0, sticky="w", pady=2
            )
            for idx, ext in enumerate(PHOTO_EXTENSIONS, start=1):
                var = tk.BooleanVar(value=False)
                self.ext_vars[ext] = var
                ttk.Checkbutton(ext_grid, text=ext, variable=var).grid(
                    row=1, column=idx, sticky="w", padx=(0, 12), pady=2
                )

            # Other / Custom row
            ttk.Label(ext_grid, text="Other:", width=10, foreground="#6D28D9").grid(
                row=2, column=0, sticky="w", pady=2
            )
            for idx, ext in enumerate(OTHER_EXTENSIONS, start=1):
                var = tk.BooleanVar(value=False)
                self.ext_vars[ext] = var
                ttk.Checkbutton(ext_grid, text=ext, variable=var).grid(
                    row=2, column=idx, sticky="w", padx=(0, 12), pady=2
                )

            ttk.Label(ext_grid, text="Custom:").grid(row=2, column=5, sticky="e", padx=(8, 4), pady=2)
            self.custom_ext_var = tk.StringVar(value="")
            custom_entry = ttk.Entry(ext_grid, textvariable=self.custom_ext_var, width=16)
            custom_entry.grid(row=2, column=6, columnspan=3, sticky="w", pady=2)

            ttk.Separator(filter_frame, orient="horizontal").pack(fill="x", pady=6)

            # 2b. File Size (MiB) & Date Range (Start Date & End Date)
            size_date_row = ttk.Frame(filter_frame)
            size_date_row.pack(fill="x", pady=(2, 0))

            # File Size box ending in MiB
            size_box = ttk.Frame(size_date_row)
            size_box.pack(side="left", padx=(0, 24))
            ttk.Label(size_box, text="Min File Size:", font=("Segoe UI", 9, "bold")).pack(side="left", padx=(0, 6))
            self.size_mib_var = tk.StringVar(value=str(int(FILE_SIZE_THRESHOLD_MIB) if FILE_SIZE_THRESHOLD_MIB.is_integer() else FILE_SIZE_THRESHOLD_MIB))
            self.size_entry = ttk.Entry(size_box, textvariable=self.size_mib_var, width=9, justify="right", font=("Segoe UI", 9))
            self.size_entry.pack(side="left")
            ttk.Label(size_box, text="MiB", font=("Segoe UI", 9, "bold")).pack(side="left", padx=(4, 8))

            # Quick size presets
            for preset in ("1", "50", "100", "500"):
                ttk.Button(
                    size_box,
                    text=f"{preset}",
                    width=4,
                    command=lambda p=preset: self.size_mib_var.set(p),
                ).pack(side="left", padx=1)

            # Start Date & End Date pickers
            dates_box = ttk.Frame(size_date_row)
            dates_box.pack(side="right")

            self.start_date_picker = DatePickerField(
                dates_box,
                label_text="Start Date:",
                default_enabled=bool(DATE_RANGE_START),
                default_date=DATE_RANGE_START or date.today().strftime("%Y-%m-%d"),
            )
            self.start_date_picker.pack(side="left", padx=(0, 14))

            self.end_date_picker = DatePickerField(
                dates_box,
                label_text="End Date:",
                default_enabled=bool(DATE_RANGE_END),
                default_date=DATE_RANGE_END or date.today().strftime("%Y-%m-%d"),
            )
            self.end_date_picker.pack(side="left")

            # 3. Destination & Action Mode Frame
            dest_frame = ttk.LabelFrame(
                outer, text=" 3. Destination & Transfer Mode ", style="Section.TLabelframe", padding=10
            )
            dest_frame.pack(fill="x", pady=(0, 8))

            dest_row = ttk.Frame(dest_frame)
            dest_row.pack(fill="x", pady=(0, 6))
            ttk.Label(dest_row, text="Destination:", width=11).pack(side="left")
            default_dest = FINAL_DESTINATION_DIR or LOCAL_TEMP_DIR
            self.dest_dir_var = tk.StringVar(value=default_dest)
            ttk.Entry(dest_row, textvariable=self.dest_dir_var, font=("Segoe UI", 9)).pack(
                side="left", fill="x", expand=True, padx=(0, 6)
            )
            ttk.Button(dest_row, text="📂 Browse...", command=self._browse_destination).pack(
                side="left", padx=(0, 4)
            )
            ttk.Button(dest_row, text="↗ Open", command=self._open_destination).pack(side="left")

            mode_row = ttk.Frame(dest_frame)
            mode_row.pack(fill="x")
            self.delete_after_copy_var = tk.BooleanVar(value=True)
            ttk.Checkbutton(
                mode_row,
                text="Delete original files from iOS device after copy & 100% size verification (asks for confirmation)",
                variable=self.delete_after_copy_var,
            ).pack(side="left")

            # Action Buttons Row
            action_bar = ttk.Frame(outer)
            action_bar.pack(fill="x", pady=(0, 8))

            self.scan_media_btn = ttk.Button(
                action_bar,
                text="🔍 Scan Media on Device",
                style="Accent.TButton",
                command=self.start_media_scan,
            )
            self.scan_media_btn.pack(side="left", padx=(0, 8))

            self.transfer_btn = ttk.Button(
                action_bar,
                text="🚀 Move / Copy Selected Files",
                style="Accent.TButton",
                command=self.start_transfer,
                state="disabled",
            )
            self.transfer_btn.pack(side="left", padx=(0, 8))

            self.cancel_btn = ttk.Button(
                action_bar,
                text="⏹ Cancel",
                command=self.request_cancel,
                state="disabled",
            )
            self.cancel_btn.pack(side="left")

            self.summary_var = tk.StringVar(value="Click 'Scan Media on Device' to preview matching files.")
            ttk.Label(action_bar, textvariable=self.summary_var, font=("Segoe UI", 9, "bold")).pack(
                side="right", padx=4
            )

            # 4. Preview Table & Activity Log (PanedWindow)
            paned = ttk.PanedWindow(outer, orient="vertical")
            paned.pack(fill="both", expand=True)

            # 4a. Matching Files Table
            table_frame = ttk.LabelFrame(
                paned,
                text=" Matching Files on Device (Click a row to toggle selection) ",
                style="Section.TLabelframe",
                padding=6,
            )
            paned.add(table_frame, weight=3)

            table_toolbar = ttk.Frame(table_frame)
            table_toolbar.pack(fill="x", pady=(0, 4))
            ttk.Button(table_toolbar, text="Check All Files", command=lambda: self._toggle_all_files(True)).pack(
                side="left", padx=(0, 4)
            )
            ttk.Button(table_toolbar, text="Uncheck All Files", command=lambda: self._toggle_all_files(False)).pack(
                side="left"
            )

            columns = ("selected", "filename", "folder", "ext", "size_mib", "mtime")
            self.tree = ttk.Treeview(
                table_frame,
                columns=columns,
                show="headings",
                selectmode="browse",
                height=8,
            )
            self.tree.heading("selected", text="Pick")
            self.tree.heading("filename", text="File Name")
            self.tree.heading("folder", text="DCIM Folder")
            self.tree.heading("ext", text="Type")
            self.tree.heading("size_mib", text="Size (MiB)")
            self.tree.heading("mtime", text="Date Modified")

            self.tree.column("selected", width=50, anchor="center", stretch=False)
            self.tree.column("filename", width=250, anchor="w")
            self.tree.column("folder", width=120, anchor="w", stretch=False)
            self.tree.column("ext", width=80, anchor="center", stretch=False)
            self.tree.column("size_mib", width=110, anchor="e", stretch=False)
            self.tree.column("mtime", width=160, anchor="center", stretch=False)

            tree_scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
            self.tree.configure(yscrollcommand=tree_scroll.set)
            self.tree.pack(side="left", fill="both", expand=True)
            tree_scroll.pack(side="right", fill="y")

            self.tree.bind("<ButtonRelease-1>", self._on_tree_click)

            # 4b. Progress & Activity Log
            bottom_frame = ttk.LabelFrame(
                paned, text=" Progress & Activity Log ", style="Section.TLabelframe", padding=6
            )
            paned.add(bottom_frame, weight=2)

            prog_row = ttk.Frame(bottom_frame)
            prog_row.pack(fill="x", pady=(0, 4))

            self.progress_var = tk.DoubleVar(value=0.0)
            self.progress_bar = ttk.Progressbar(
                prog_row, variable=self.progress_var, maximum=100.0, mode="determinate"
            )
            self.progress_bar.pack(side="left", fill="x", expand=True, padx=(0, 8))

            self.progress_label_var = tk.StringVar(value="Idle")
            ttk.Label(prog_row, textvariable=self.progress_label_var, width=36, anchor="e").pack(side="right")

            log_container = ttk.Frame(bottom_frame)
            log_container.pack(fill="both", expand=True)

            self.log_text = tk.Text(
                log_container,
                height=6,
                wrap="word",
                font=("Consolas", 9),
                bg="#0F172A",
                fg="#E2E8F0",
                insertbackground="#E2E8F0",
                state="disabled",
            )
            log_scroll = ttk.Scrollbar(log_container, orient="vertical", command=self.log_text.yview)
            self.log_text.configure(yscrollcommand=log_scroll.set)
            self.log_text.pack(side="left", fill="both", expand=True)
            log_scroll.pack(side="right", fill="y")

        # --- UI Helpers ---

        def _post_ui(self, fn: Callable[[], None]) -> None:
            self.ui_queue.put(fn)

        def _process_ui_queue(self) -> None:
            try:
                while True:
                    fn = self.ui_queue.get_nowait()
                    fn()
            except queue.Empty:
                pass
            self.root.after(50, self._process_ui_queue)

        def log(self, message: str) -> None:
            timestamp = datetime.now().strftime("%H:%M:%S")
            line = f"[{timestamp}] {message}\n"

            def _append() -> None:
                self.log_text.configure(state="normal")
                self.log_text.insert("end", line)
                self.log_text.see("end")
                self.log_text.configure(state="disabled")

            self._post_ui(_append)

        def _set_busy(self, busy: bool) -> None:
            self.is_busy = busy
            state_normal = "disabled" if busy else "normal"
            self.scan_dev_btn.configure(state=state_normal)
            self.scan_media_btn.configure(state=state_normal)
            self.transfer_btn.configure(
                state="normal" if (not busy and any(f.selected for f in self.scanned_files)) else "disabled"
            )
            self.cancel_btn.configure(state="normal" if busy else "disabled")

        def request_cancel(self) -> None:
            if self.is_busy:
                self.cancel_event.set()
                self.log("Cancellation requested... waiting for current operation to stop safely.")

        # --- Extension Filter Helpers ---

        def _select_videos_only(self) -> None:
            for ext, var in self.ext_vars.items():
                var.set(ext in VIDEO_EXTENSIONS)

        def _select_photos_only(self) -> None:
            for ext, var in self.ext_vars.items():
                var.set(ext in PHOTO_EXTENSIONS)

        def _select_all_extensions(self) -> None:
            for var in self.ext_vars.values():
                var.set(True)

        def _clear_all_extensions(self) -> None:
            for var in self.ext_vars.values():
                var.set(False)

        def _get_selected_extensions(self) -> Set[str]:
            selected = {ext.upper() for ext, var in self.ext_vars.items() if var.get()}
            custom_raw = self.custom_ext_var.get().strip()
            if custom_raw:
                for part in custom_raw.replace(";", ",").replace(" ", ",").split(","):
                    cleaned = part.strip()
                    if cleaned:
                        if not cleaned.startswith("."):
                            cleaned = "." + cleaned
                        selected.add(cleaned.upper())
            return selected

        # --- Destination Helpers ---

        def _browse_destination(self) -> None:
            initial = self.dest_dir_var.get().strip() or os.path.expanduser("~")
            chosen = filedialog.askdirectory(
                parent=self.root,
                title="Select Destination Folder for iOS Media",
                initialdir=initial if os.path.isdir(initial) else os.path.expanduser("~"),
            )
            if chosen:
                self.dest_dir_var.set(os.path.normpath(chosen))

        def _open_destination(self) -> None:
            path = self.dest_dir_var.get().strip()
            if not path:
                return
            os.makedirs(path, exist_ok=True)
            if sys.platform == "win32":
                os.startfile(path)

        # --- Device Scanning ---

        def scan_devices(self) -> None:
            if self.is_busy:
                return
            self.device_status_var.set("Scanning for connected USB iOS devices...")
            self.log("Scanning for connected iOS devices...")

            def _worker() -> None:
                try:
                    devs = self.service.list_devices()
                    self._post_ui(lambda: self._on_devices_scanned(devs, None))
                except Exception as exc:
                    self._post_ui(lambda e=exc: self._on_devices_scanned([], str(e)))

            threading.Thread(target=_worker, daemon=True).start()

        def _on_devices_scanned(self, devs: List[DeviceInfo], error_msg: Optional[str]) -> None:
            self.devices = devs
            if error_msg:
                self.device_combo["values"] = []
                self.device_var.set("")
                self.device_status_var.set(f"Error: {error_msg}")
                self.log(f"Device scan error: {error_msg}")
                return

            if not devs:
                self.device_combo["values"] = []
                self.device_var.set("")
                self.device_status_var.set(
                    "No iOS device detected. Connect your iPhone/iPad via USB, unlock it, and tap 'Trust'."
                )
                self.log("No iOS device found. Ensure Apple Devices is installed and your device is unlocked.")
                return

            labels = [d.display_name for d in devs]
            self.device_combo["values"] = labels
            # If there is just one device (or multiple), automatically select the first as default
            self.device_combo.current(0)
            self._on_device_selected()
            if len(devs) == 1:
                self.log(f"Automatically selected single connected device: {devs[0].display_name}")
            else:
                self.log(f"Found {len(devs)} devices. Defaulted to: {devs[0].display_name}")

        def _get_active_device(self) -> Optional[DeviceInfo]:
            if not self.devices:
                return None
            idx = self.device_combo.current()
            if 0 <= idx < len(self.devices):
                return self.devices[idx]
            return self.devices[0]

        def _on_device_selected(self, _event=None) -> None:
            dev = self._get_active_device()
            if not dev:
                return
            storage_txt = ""
            if dev.free_bytes is not None and dev.total_bytes is not None and dev.total_bytes > 0:
                free_gib = dev.free_bytes / (1024 ** 3)
                total_gib = dev.total_bytes / (1024 ** 3)
                storage_txt = f" | Storage Free: {free_gib:.1f} GiB / {total_gib:.1f} GiB"
            model_txt = f" | Model: {dev.model}" if dev.model else ""
            self.device_status_var.set(f"Connected: {dev.name} (UDID: {dev.udid}){model_txt}{storage_txt}")

        # --- Media Scanning & Preview Table ---

        def _build_scan_filter(self) -> Optional[ScanFilter]:
            exts = self._get_selected_extensions()
            if not exts:
                messagebox.showwarning(
                    "No Media Type Selected",
                    "Please check at least one Media Type extension (e.g. .MOV, .MP4).",
                    parent=self.root,
                )
                return None

            raw_size = self.size_mib_var.get().strip()
            try:
                min_mib = float(raw_size) if raw_size else 0.0
                if min_mib < 0:
                    raise ValueError
            except ValueError:
                messagebox.showerror(
                    "Invalid File Size",
                    f"Please enter a valid non-negative number in MiB (got '{raw_size}').",
                    parent=self.root,
                )
                return None

            try:
                start_ts, end_ts = parse_date_bounds(
                    self.start_date_picker.get_date_str(),
                    self.end_date_picker.get_date_str(),
                )
            except ValueError as exc:
                messagebox.showerror(
                    "Invalid Date Range",
                    f"Please enter valid dates in YYYY-MM-DD format.\n\nDetails: {exc}",
                    parent=self.root,
                )
                return None

            return ScanFilter(
                remote_root=REMOTE_MEDIA_DIR,
                extensions=exts,
                min_size_bytes=int(min_mib * 1024 * 1024),
                start_ts=start_ts,
                end_ts=end_ts,
            )

        def start_media_scan(self) -> None:
            if self.is_busy:
                return

            dev = self._get_active_device()
            if dev is None:
                self.scan_devices()
                dev = self._get_active_device()
                if dev is None:
                    messagebox.showwarning(
                        "No Device Connected",
                        "Please connect an iOS device via USB and click 'Scan Devices'.",
                        parent=self.root,
                    )
                    return

            scan_filter = self._build_scan_filter()
            if scan_filter is None:
                return

            self.cancel_event.clear()
            self._set_busy(True)
            self.progress_var.set(0)
            self.progress_label_var.set("Scanning device...")

            exts_str = ", ".join(sorted(scan_filter.extensions))
            min_mib = scan_filter.min_size_bytes / (1024 * 1024)
            self.log(f"Starting media scan on {dev.display_name} (Extensions: {exts_str}, Min Size: {min_mib:.2f} MiB)...")

            def on_scan_progress(status_text: str, current: int, total: int) -> None:
                pct = (current / total * 100.0) if total > 0 else 0.0
                self._post_ui(lambda: self._update_progress_ui(pct, status_text))

            def _worker() -> None:
                try:
                    files = self.service.scan_media(
                        dev.udid,
                        scan_filter=scan_filter,
                        log_cb=self.log,
                        progress_cb=on_scan_progress,
                        cancel_event=self.cancel_event,
                    )
                    self._post_ui(lambda: self._on_media_scan_complete(files, None))
                except Exception as exc:
                    self._post_ui(lambda e=exc: self._on_media_scan_complete([], str(e)))

            threading.Thread(target=_worker, daemon=True).start()

        def _update_progress_ui(self, pct: float, label_text: str) -> None:
            self.progress_var.set(max(0.0, min(100.0, pct)))
            self.progress_label_var.set(label_text)

        def _on_media_scan_complete(self, files: List[RemoteMediaFile], error_msg: Optional[str]) -> None:
            self.scanned_files = files
            self._populate_tree()
            self._set_busy(False)

            if error_msg:
                self.progress_label_var.set("Scan failed")
                self.log(f"Error scanning media: {error_msg}")
                messagebox.showerror("Media Scan Failed", error_msg, parent=self.root)
                return

            total_mib = sum(f.size_mib for f in files)
            self.progress_var.set(100.0 if files else 0.0)
            self.progress_label_var.set(f"Found {len(files)} file(s) ({total_mib:.1f} MiB)")
            self.log(f"Scan finished: {len(files)} matching file(s), total {total_mib:.2f} MiB ({total_mib / 1024:.2f} GiB).")

        def _populate_tree(self) -> None:
            self.tree.delete(*self.tree.get_children())
            for idx, f in enumerate(self.scanned_files):
                chk = "☑" if f.selected else "☐"
                self.tree.insert(
                    "",
                    "end",
                    iid=str(idx),
                    values=(chk, f.filename, f.folder, f.extension, f"{f.size_mib:,.2f}", f.mtime_str),
                )
            self._update_selection_summary()

        def _on_tree_click(self, event) -> None:
            if self.is_busy:
                return
            row_id = self.tree.identify_row(event.y)
            if not row_id:
                return
            idx = int(row_id)
            if 0 <= idx < len(self.scanned_files):
                item = self.scanned_files[idx]
                item.selected = not item.selected
                chk = "☑" if item.selected else "☐"
                self.tree.item(
                    row_id,
                    values=(chk, item.filename, item.folder, item.extension, f"{item.size_mib:,.2f}", item.mtime_str),
                )
                self._update_selection_summary()

        def _toggle_all_files(self, select: bool) -> None:
            if self.is_busy:
                return
            for f in self.scanned_files:
                f.selected = select
            self._populate_tree()

        def _update_selection_summary(self) -> None:
            selected = [f for f in self.scanned_files if f.selected]
            total_mib = sum(f.size_mib for f in selected)
            total_gib = total_mib / 1024.0
            self.summary_var.set(
                f"Selected: {len(selected)} / {len(self.scanned_files)} files  |  "
                f"Size: {total_mib:,.2f} MiB ({total_gib:.2f} GiB)"
            )
            if not self.is_busy:
                self.transfer_btn.configure(state="normal" if selected else "disabled")

        # --- File Transfer, Verification & Deletion ---

        def start_transfer(self) -> None:
            if self.is_busy:
                return

            dev = self._get_active_device()
            if dev is None:
                messagebox.showwarning("No Device", "No active iOS device selected.", parent=self.root)
                return

            selected_files = [f for f in self.scanned_files if f.selected]
            if not selected_files:
                messagebox.showinfo("No Files Selected", "Please select at least one file to transfer.", parent=self.root)
                return

            dest_dir = self.dest_dir_var.get().strip() or LOCAL_TEMP_DIR
            delete_after = self.delete_after_copy_var.get()

            self.cancel_event.clear()
            self._set_busy(True)
            self.progress_var.set(0.0)
            self.progress_label_var.set("Starting file transfer...")

            def on_copy_progress(
                file_idx: int,
                total_files: int,
                bytes_done: int,
                total_bytes: int,
                speed_mibs: float,
                filename: str,
            ) -> None:
                pct = (bytes_done / total_bytes * 100.0) if total_bytes > 0 else 100.0
                status = f"[{file_idx}/{total_files}] {filename} ({pct:.1f}% • {speed_mibs:.1f} MiB/s)"
                self._post_ui(lambda: self._update_progress_ui(pct, status))

            def _worker() -> None:
                try:
                    verified, local_paths = self.service.copy_and_verify_files(
                        udid=dev.udid,
                        files_to_copy=selected_files,
                        staging_dir=LOCAL_TEMP_DIR,
                        log_cb=self.log,
                        progress_cb=on_copy_progress,
                        cancel_event=self.cancel_event,
                    )
                    self._post_ui(
                        lambda: self._on_copy_finished(
                            dev=dev,
                            selected_files=selected_files,
                            verified=verified,
                            local_paths=local_paths,
                            dest_dir=dest_dir,
                            delete_after=delete_after,
                            error_msg=None,
                        )
                    )
                except InterruptedError:
                    self._post_ui(
                        lambda: self._on_copy_finished(
                            dev=dev,
                            selected_files=selected_files,
                            verified=False,
                            local_paths={},
                            dest_dir=dest_dir,
                            delete_after=delete_after,
                            error_msg="Transfer cancelled by user.",
                        )
                    )
                except Exception as exc:
                    self._post_ui(
                        lambda e=exc: self._on_copy_finished(
                            dev=dev,
                            selected_files=selected_files,
                            verified=False,
                            local_paths={},
                            dest_dir=dest_dir,
                            delete_after=delete_after,
                            error_msg=str(e),
                        )
                    )

            threading.Thread(target=_worker, daemon=True).start()

        def _on_copy_finished(
            self,
            dev: DeviceInfo,
            selected_files: List[RemoteMediaFile],
            verified: bool,
            local_paths: Dict[str, str],
            dest_dir: str,
            delete_after: bool,
            error_msg: Optional[str],
        ) -> None:
            if error_msg or not verified:
                self._set_busy(False)
                self.progress_label_var.set("Transfer incomplete")
                msg = error_msg or "File verification failed! Original files on the iPhone were NOT touched."
                self.log(msg)
                messagebox.showwarning("Transfer Stopped", msg, parent=self.root)
                return

            total_mib = sum(f.size_mib for f in selected_files)

            # Confirm deletion if Move mode is checked
            should_delete = False
            if delete_after:
                should_delete = messagebox.askyesno(
                    "Confirm Deletion from iOS Device",
                    f"All {len(selected_files)} file(s) ({total_mib:,.2f} MiB) have been copied and 100% verified on your PC.\n\n"
                    f"Do you want to DELETE the original {len(selected_files)} file(s) from '{dev.name}' now?",
                    parent=self.root,
                )

            def _finalize_worker() -> None:
                try:
                    if should_delete:
                        self.service.delete_files_from_device(
                            dev.udid, selected_files, log_cb=self.log
                        )
                    else:
                        self.log("Kept original files on iOS device.")

                    final_dir = self.service.move_staged_to_destination(
                        local_paths=local_paths,
                        staging_dir=LOCAL_TEMP_DIR,
                        final_destination_dir=dest_dir,
                        log_cb=self.log,
                    )
                    self._post_ui(lambda: self._on_finalize_complete(len(selected_files), total_mib, final_dir, should_delete, None))
                except Exception as exc:
                    self._post_ui(lambda e=exc: self._on_finalize_complete(0, 0.0, dest_dir, should_delete, str(e)))

            threading.Thread(target=_finalize_worker, daemon=True).start()

        def _on_finalize_complete(
            self,
            count: int,
            total_mib: float,
            final_dir: str,
            deleted_originals: bool,
            error_msg: Optional[str],
        ) -> None:
            self._set_busy(False)
            if error_msg:
                self.progress_label_var.set("Error during finalization")
                self.log(f"Error: {error_msg}")
                messagebox.showerror("Error", error_msg, parent=self.root)
                return

            self.progress_var.set(100.0)
            action_word = "Moved" if deleted_originals else "Copied"
            self.progress_label_var.set(f"{action_word} {count} file(s) successfully!")
            self.log(f"=== Process Complete! {action_word} {count} file(s) ({total_mib:,.2f} MiB) to '{final_dir}' ===")

            if deleted_originals:
                # Remove deleted files from the current preview list
                self.scanned_files = [f for f in self.scanned_files if not f.selected]
                self._populate_tree()
                self.scan_devices()

            messagebox.showinfo(
                "Transfer Complete",
                f"Successfully {action_word.lower()} {count} file(s) ({total_mib:,.2f} MiB) to:\n{final_dir}",
                parent=self.root,
            )

    root = tk.Tk()
    IOSMediaMoverApp(root)
    root.mainloop()


# --- Entry Point ---

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="iOS Media Mover - Move heavy videos and media from iOS devices to Windows."
    )
    parser.add_argument(
        "--cli",
        action="store_true",
        help="Run in command-line mode instead of launching the graphical UI.",
    )
    parser.add_argument(
        "-u", "--udid",
        type=str,
        default=None,
        help="Target specific iOS device UDID (defaults to first connected device).",
    )
    parser.add_argument(
        "-e", "--extensions",
        nargs="+",
        default=None,
        help="File extensions to include (e.g. .MOV .MP4 .HEIC). Defaults to all video formats.",
    )
    parser.add_argument(
        "-s", "--min-size-mib",
        type=float,
        default=FILE_SIZE_THRESHOLD_MIB,
        help=f"Minimum file size in MiB (default: {FILE_SIZE_THRESHOLD_MIB}).",
    )
    parser.add_argument(
        "--start-date",
        type=str,
        default=DATE_RANGE_START,
        help="Start date filter in YYYY-MM-DD format.",
    )
    parser.add_argument(
        "--end-date",
        type=str,
        default=DATE_RANGE_END,
        help="End date filter in YYYY-MM-DD format (inclusive).",
    )
    parser.add_argument(
        "-d", "--destination",
        type=str,
        default=FINAL_DESTINATION_DIR,
        help="Final destination directory on Windows.",
    )
    parser.add_argument(
        "--copy-only",
        action="store_true",
        help="Copy and verify files without deleting originals from the iOS device.",
    )
    return parser


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()
    if args.cli:
        run_cli_mode(args)
    else:
        launch_gui()


if __name__ == "__main__":
    main()

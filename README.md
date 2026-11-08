# iOS Media Mover

**iOS Media Mover** is a Windows utility that helps you quickly scan, preview, copy, verify, and move large media files (such as 4K/ProRes videos and photos) from an iPhone or iPad to your Windows PC over USB.

It verifies the exact byte size of every copied file on your PC before asking whether to delete the original files from your iOS device.

---

## Prerequisites

1. Install **[Apple Devices](https://apps.microsoft.com/detail/9np83lwlpz9k?hl=en-US&gl=US)** from the Microsoft Store (provides the Windows USB driver required to communicate with iOS devices).
2. Connect your iPhone or iPad to your PC via USB, unlock the device, and tap **Trust This Computer** when prompted.

---

## Installation

### Option A: Download Pre-Built Release (Recommended)
1. Download the latest `iOSMediaMover-Windows-x64.zip` from the **[Releases](../../releases)** page.
2. Extract the ZIP folder anywhere on your PC.

### Option B: Run from Source
If running directly from source (`move_ios_media.py`), install [Python 3.9+](https://www.python.org/) and ensure `libimobiledevice` binaries are located in `./bin` or `C:/Program Files/libimobiledevice/mingw64/bin` (or run `./scripts/build_release.ps1` to pull and build them automatically).

---

## Using the Graphical UI

Launch the application by double-clicking **`iOSMediaMover.exe`** (from the Release folder) or by running:

```powershell
python move_ios_media.py
```

### UI Controls
1. **iOS Device**:
   - The app automatically scans for connected USB devices on startup. If a single device is connected, it is selected automatically.
   - Click **🔄 Scan Devices** at any time to refresh the device list.
2. **Media Type (Extensions)**:
   - Select one or more file extensions to include.
   - By default, **all video formats** (`.MOV`, `.MP4`, `.M4V`, `.3GP`) are checked. You can also check photo/audio formats (`.HEIC`, `.JPG`, `.JPEG`, `.PNG`, `.DNG`, `.GIF`, `.TIF`, `.TIFF`, `.AAE`, `.M4A`, `.WAV`, `.MP3`) or enter custom extensions.
3. **Min File Size (`MiB`)**:
   - Enter the minimum file size threshold in **MiB** (for example, `100` MiB to only find videos 100 MiB or larger).
4. **Start Date & End Date**:
   - Optionally check **Start Date** and/or **End Date** and click **📅 Pick...** to choose a date range from the calendar (`YYYY-MM-DD`).
5. **Destination & Transfer**:
   - Click **📂 Browse...** to choose the folder on your PC where files should be saved.
   - Click **🔍 Scan Media on Device** to preview all matching files in the table (click any row to check/uncheck individual files).
   - Click **🚀 Move / Copy Selected Files** to start transferring. Once all files are copied and 100% size-verified on disk, you will be prompted to confirm before any original files are deleted from your iPhone/iPad.

---

## Using the Command-Line Interface (CLI)

Pass `--cli` to run in command-line mode without opening the graphical window:

```powershell
# Scan and move all video files >= 100 MiB modified on or after 2026-10-30 to E:\MyVideos
python move_ios_media.py --cli --min-size-mib 100 --start-date 2026-10-30 --destination "E:/MyVideos"

# Copy specific file types within a date range without deleting originals from the iPhone
python move_ios_media.py --cli --extensions .MOV .MP4 .HEIC --min-size-mib 50 --start-date 2026-01-01 --end-date 2026-12-31 --destination "E:/MyVideos" --copy-only
```

### CLI Options
| Option | Description |
| :--- | :--- |
| `--cli` | Run in command-line mode instead of launching the UI. |
| `-u`, `--udid <UDID>` | Target a specific iOS device by UDID (defaults to the first connected device). |
| `-e`, `--extensions <EXT ...>` | File extensions to scan (defaults to `.MOV .MP4 .M4V .3GP`). |
| `-s`, `--min-size-mib <MiB>` | Minimum file size in MiB (default: `1.0`). |
| `--start-date <YYYY-MM-DD>` | Only include files modified on or after this date. |
| `--end-date <YYYY-MM-DD>` | Only include files modified on or before this date (inclusive). |
| `-d`, `--destination <PATH>` | Final destination folder on Windows. |
| `--copy-only` | Copy and verify files on PC without deleting originals from the iOS device. |

---

## License & Credits

This project is licensed under the **GNU Lesser General Public License v2.1 (or later)** — see [`LICENSE`](./LICENSE) and [`THIRD_PARTY_NOTICES.md`](./THIRD_PARTY_NOTICES.md) for details.

- Device communication is powered by [libimobiledevice](https://libimobiledevice.org/) (`LGPL-2.1-or-later`).
- Apple, iPhone, iPad, iOS, and iPadOS are trademarks of Apple Inc. This project is independent software and has not been authorized, sponsored, or otherwise approved by Apple Inc.

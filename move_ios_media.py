# c:\Users\yuanw\Code\iOSMediaMover\iOSMediaMover\move_ios_media.py
from multiprocessing.util import info
import subprocess
import os
import json
import shutil
import sys
from datetime import datetime

# --- Configuration ---

# IMPORTANT: Update this path to where you installed libimobiledevice.
# This script assumes it's being run from a shell (like mingw64)
# that has the necessary environment set up for these executables.
LIBIMOBILEDEVICE_PATH = "C:/Program Files/libimobiledevice/mingw64/bin"
IDEVICE_ID_EXE = os.path.join(LIBIMOBILEDEVICE_PATH, "idevice_id.exe")
AFCCLIENT_EXE = os.path.join(LIBIMOBILEDEVICE_PATH, "afcclient.exe")

# The root directory on the iPhone where photos and videos are stored.
REMOTE_MEDIA_DIR = "DCIM"

# Local temporary directory to store files before moving them to their final destination.
# It will be created in the same directory where the script is run.
LOCAL_TEMP_DIR = os.path.join(os.path.dirname(__file__), "temp_iphone_media")

# Optional: Set a final destination for the media files.
# If set to None, files will remain in LOCAL_TEMP_DIR.
# Example: "E:/MyVideos"
FINAL_DESTINATION_DIR = None 

# File size threshold in bytes. Files larger than this will be marked for moving.
# Example: 500 MB = 500 * 1024 * 1024
FILE_SIZE_THRESHOLD_BYTES = 1 * 1024 * 1024

# File extension to look for.
VIDEO_FILE_EXTENSION = ".MOV"

# Date range filter (optional). If set, only files modified within this range will be considered.
DATE_RANGE_START = "2026-10-30"  # Example: "2023-01-01"
DATE_RANGE_END = None    # Example: "2023-12-31"
DATE_RANGE_START_TS = datetime.strptime(DATE_RANGE_START, "%Y-%m-%d").timestamp() if DATE_RANGE_START else None
DATE_RANGE_END_TS = datetime.strptime(DATE_RANGE_END, "%Y-%m-%d").timestamp() if DATE_RANGE_END else None

# --- Helper Functions ---

def run_command(command):
    """Runs a command and returns its output, raising an error if it fails."""
    print(f"Executing: {' '.join(command)}")
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=True,
            encoding='utf-8'
        )
        return result.stdout.strip()
    except FileNotFoundError:
        print(f"Error: Command not found: {command[0]}")
        print(f"Please ensure LIBIMOBILEDEVICE_PATH is set correctly and the command exists.")
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        print(f"Error executing command: {' '.join(command)}")
        print(f"Return Code: {e.returncode}")
        print(f"Output:\n{e.stdout}")
        print(f"Error Output:\n{e.stderr}")
        sys.exit(1)

def get_device_udid():
    """Gets the UDID of the first connected iOS device."""
    print("--> Finding connected iOS device...")
    output = run_command([IDEVICE_ID_EXE, "-l"])
    if not output:
        print("Error: No iOS device found. Please connect your device via USB.")
        sys.exit(1)
    
    # The output can contain multiple UDIDs, separated by newlines.
    udids = [udid for udid in output.split('\n') if udid]

    if len(udids) > 1:
        print(f"Warning: Multiple devices found. Using the first one: {udids[0]}")
    
    udid = udids[0]
    print(f"Found device with UDID: {udid}")
    return udid

def get_remote_file_info(udid, remote_path):
    """Gets file information (like size) for a remote file."""
    output = run_command([AFCCLIENT_EXE, "-u", udid, "info", remote_path])
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        print(f"Error: Could not parse file info for {remote_path}")
        return None

def find_videos_to_move(udid):
    """Finds video files on the device that are larger than the threshold."""
    print(f"\n--> Searching for videos larger than {FILE_SIZE_THRESHOLD_BYTES / 1024 / 1024:.2f} MB...")
    files_to_move = []
    
    # 1. List subdirectories in DCIM (e.g., 100APPLE, 101APPLE)
    dir_list_output = run_command([AFCCLIENT_EXE, "-u", udid, "ls", REMOTE_MEDIA_DIR])
    if not dir_list_output:
        print(f"Warning: No subdirectories found in {REMOTE_MEDIA_DIR}.")
        return []
        
    sub_dirs = dir_list_output.split('\n')

    # 2. List files in each subdirectory
    for sub_dir in sub_dirs:
        if not sub_dir:
            continue
        remote_dir_path = f"{REMOTE_MEDIA_DIR}/{sub_dir}"
        info = get_remote_file_info(udid, remote_dir_path)
        if not info or info.get("st_ifmt") != "S_IFDIR":
            print("WARNING: DIRECTORY ERROR: {remote_dir_path}")
            continue
        dir_ts = float(info.get("st_mtime", 0) / 1000000000)  # Convert nanoseconds to seconds
        if DATE_RANGE_START_TS is not None and dir_ts < DATE_RANGE_START_TS:
            print(f"      [DIR] {sub_dir} (Directory, skipping because not modified within date range)")
            continue


        print(f"    Scanning directory: {remote_dir_path}")
        
        file_list_output = run_command([AFCCLIENT_EXE, "-u", udid, "ls", remote_dir_path])
        if not file_list_output:
            continue
            
        files = file_list_output.split('\n')
        
        # 3. Check each file
        for filename in files:
            if filename.upper().endswith(VIDEO_FILE_EXTENSION):
                remote_file_path = f"{remote_dir_path}/{filename}"
                info = get_remote_file_info(udid, remote_file_path)
                if not info:
                    continue
                
                if info.get("st_ifmt") == "S_IFREG":
                    file_size = int(info.get("st_size", 0))
                    file_ts = float(info.get("st_mtime", 0) / 1000000000)  # Convert nanoseconds to seconds
                    large_enough = FILE_SIZE_THRESHOLD_BYTES is None or file_size >= FILE_SIZE_THRESHOLD_BYTES
                    older_than_start = DATE_RANGE_START_TS is None or file_ts >= DATE_RANGE_START_TS
                    younger_than_end = DATE_RANGE_END_TS is None or file_ts <= DATE_RANGE_END_TS
                    if large_enough and older_than_start and younger_than_end:
                        print(f"      [MARK] {filename} (Size: {file_size / 1024 / 1024:.2f} MB)")
                        files_to_move.append({"path": remote_file_path, "size": file_size})
                    else:
                        print(f"      [SKIP] {filename} (Size: {file_size / 1024 / 1024:.2f} MB)")

    return files_to_move

def copy_files_to_local(udid, files_to_move):
    """Copies files from the device to the local temporary directory."""
    if not files_to_move:
        return True

    print(f"\n--> Copying {len(files_to_move)} files to temporary directory: {LOCAL_TEMP_DIR}")
    os.makedirs(LOCAL_TEMP_DIR, exist_ok=True)
    
    for file_info in files_to_move:
        remote_path = file_info["path"]
        filename = os.path.basename(remote_path)
        local_path = os.path.join(LOCAL_TEMP_DIR, filename)
        
        print(f"    Copying {remote_path} to {local_path}...")
        run_command([AFCCLIENT_EXE, "-u", udid, "get", remote_path, local_path])

    # Verification step
    print("\n--> Verifying copied files...")
    all_verified = True
    for file_info in files_to_move:
        filename = os.path.basename(file_info["path"])
        local_path = os.path.join(LOCAL_TEMP_DIR, filename)
        if os.path.exists(local_path) and os.path.getsize(local_path) == file_info["size"]:
            print(f"    [OK] {filename}")
        else:
            print(f"    [FAIL] Verification failed for {filename}!")
            all_verified = False
            
    return all_verified

def delete_files_from_device(udid, files_to_delete):
    """Deletes files from the iOS device."""
    if not files_to_delete:
        return
        
    print("\n--> Deleting files from device...")
    for file_info in files_to_delete:
        remote_path = file_info["path"]
        print(f"    Deleting {remote_path}...")
        run_command([AFCCLIENT_EXE, "-u", udid, "rm", remote_path])
    print("Deletion complete.")

def move_files_to_final_destination():
    """Moves files from the temp directory to the final destination."""
    if not FINAL_DESTINATION_DIR:
        print(f"\n--> Files are stored in the temporary directory: {LOCAL_TEMP_DIR}")
        return

    print(f"\n--> Moving files to final destination: {FINAL_DESTINATION_DIR}")
    os.makedirs(FINAL_DESTINATION_DIR, exist_ok=True)
    
    files_in_temp = os.listdir(LOCAL_TEMP_DIR)
    for filename in files_in_temp:
        src_path = os.path.join(LOCAL_TEMP_DIR, filename)
        dest_path = os.path.join(FINAL_DESTINATION_DIR, filename)
        
        if os.path.exists(dest_path):
            print(f"Warning: File {filename} already exists in destination. Skipping.")
            continue
            
        print(f"    Moving {filename}...")
        shutil.move(src_path, dest_path)
        
    # Clean up the now-empty temporary directory
    try:
        os.rmdir(LOCAL_TEMP_DIR)
        print(f"Cleaned up temporary directory: {LOCAL_TEMP_DIR}")
    except OSError as e:
        print(f"Warning: Could not remove temporary directory {LOCAL_TEMP_DIR}. It may not be empty. Error: {e}")

# --- Main Execution ---

def main():
    """Main script logic."""
    try:
        udid = get_device_udid()
        files_to_move = find_videos_to_move(udid)

        if not files_to_move:
            print("\nNo large video files found to move. Exiting.")
            return

        total_size_gb = sum(f['size'] for f in files_to_move) / (1024**3)
        print(f"\nFound {len(files_to_move)} files to move, totaling {total_size_gb:.2f} GB.")

        if not copy_files_to_local(udid, files_to_move):
            print("\nVerification of copied files failed. Aborting before deletion.")
            print("Please check the files in the temp directory and resolve any issues.")
            sys.exit(1)
        
        print("\nAll files copied and verified successfully.")
        
        # Confirmation step
        while True:
            confirm = input("Do you want to delete the original files from the iPhone? (yes/no): ").lower()
            if confirm in ["yes", "no"]:
                break
            print("Invalid input. Please enter 'yes' or 'no'.")

        if confirm == "yes":
            delete_files_from_device(udid, files_to_move)
            move_files_to_final_destination()
            print("\nProcess complete!")
        else:
            print("\nAborted. Original files were not deleted from the device.")
            print(f"Copied files are available in: {LOCAL_TEMP_DIR}")

    except Exception as e:
        print(f"\nAn unexpected error occurred: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()

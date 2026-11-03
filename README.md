# Prerequisite
- Install `Apple Devices` from Microsoft Store: https://apps.microsoft.com/detail/9np83lwlpz9k?hl=en-US&gl=US
- Install Msys2
- Install https://github.com/libimobiledevice/libimobiledevice and all its dependencies

The program assumes libimobiledevice executables are installed at `C:/Program Files/libimobiledevice/mingw64/bin`.
Specifically it requires the `afcclient.exe` and `idevice_id.exe`.

Please connect one iOS device at a time for better recognition.

# Running

Edit the variables at the top of the `move_ios_media.py` file. Mainly:

- `REMOTE_MEDIA_DIR = "DCIM"` this assumes the media files are stored at the DCIM folder. This should be true by default.
- `FILE_SIZE_THRESHOLD_BYTES = 1 * 1024 * 1024` the program only scans for file beyond this threshold.
- `VIDEO_FILE_EXTENSION = ".MOV"` the extension of the videos; should be MOV by default.
- `DATE_RANGE_START = "2026-10-30"` and `DATE_RANGE_END = None`, a date range between which the files are scanned.

Then run

`python move_ios_media.py`


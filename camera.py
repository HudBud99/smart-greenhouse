# =============================================================================
# camera.py — PHOTO CAPTURE AND SCHEDULING
# =============================================================================
# Handles all Raspberry Pi Camera functionality:
#   - Capturing photos at scheduled intervals (day/night rates)
#   - Saving photos to the USB jump drive (NOT the SD card)
#   - Maintaining a "latest.jpg" symlink for the feh image viewer
#   - Running as a daemon thread alongside the sensor and irrigation loops
#
# WHY SAVE TO USB DRIVE INSTEAD OF SD CARD?
#   SD cards have a limited number of write cycles before they fail.
#   Writing a new photo every 15 minutes = 96 photos/day = ~35,000/year.
#   This would wear out a cheap SD card within months. A USB jump drive
#   uses higher-endurance flash and is cheap/easy to replace if it fails.
#   The SD card only holds the OS and code — much safer long-term.
#
# HOW feh WORKS WITH THIS:
#   feh is launched separately (see SETUP.sh) with --reload 60, pointing at
#   config.LATEST_PHOTO. Every time a new photo is captured, this module
#   updates the symlink at LATEST_PHOTO to point to the new file.
#   feh automatically shows the updated photo on its next reload cycle.
#   This gives you a live camera feed on the TV without any streaming overhead.
#   Launch command: feh --reload 60 --scale-down --auto-zoom /mnt/usb/photos/latest.jpg
#
# CAMERA COMMAND:
#   Uses libcamera-still, which is the standard camera tool for
#   Raspberry Pi OS Bullseye and later. If you're running Buster or older,
#   replace "libcamera-still" with "raspistill" in capture_photo().
# =============================================================================

import os
import subprocess
import datetime
import time

import config
import db


def capture_photo(label="scheduled"):
    """
    Captures one photo and saves it to the USB jump drive.
    Also updates the latest.jpg symlink so feh auto-refreshes on the TV.

    label: string prefix for the filename (e.g. "day", "night", "scheduled")
           Helps identify photos by context when browsing the folder.

    Filename format: {label}_{YYYYMMDD}_{HHMMSS}.jpg
    Example: day_20260409_143200.jpg

    Returns:
        str: full path to the saved file on success
        None: if capture failed for any reason (logged to DB)

    The subprocess call has a hard 20-second timeout. If libcamera-still
    hangs (sometimes happens with bad ribbon cable connections), it won't
    block the camera loop indefinitely.
    """
    # Ensure the photo directory exists (creates it if this is the first run)
    os.makedirs(config.PHOTO_DIR, exist_ok=True)

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename  = os.path.join(config.PHOTO_DIR, f"{label}_{timestamp}.jpg")

    try:
        # Call libcamera-still to capture one frame
        # --output    : save path
        # --nopreview : don't open a preview window (no desktop = no issue)
        # --timeout   : 2000ms sensor warmup before capture (improves exposure)
        # --width/height: 1080p resolution
        # --quality   : JPEG compression 85% — good quality, reasonable file size
        subprocess.run(
            [
                "libcamera-still",
                "--output",    filename,
                "--nopreview",
                "--timeout",   "2000",
                "--width",     "1920",
                "--height",    "1080",
                "--quality",   "85",
            ],
            check=True,           # Raises CalledProcessError if exit code != 0
            timeout=20,           # Hard timeout — won't hang longer than 20s
            capture_output=True,  # Captures stdout/stderr for error messages
        )

        # Update the symlink so feh shows this new photo on next reload
        _update_latest_symlink(filename)
        return filename

    except subprocess.TimeoutExpired:
        db.log_error("camera", "libcamera-still timed out after 20 seconds — check camera ribbon cable")
        return None

    except subprocess.CalledProcessError as e:
        # Decode the stderr bytes to get a readable error message
        stderr_msg = e.stderr.decode(errors="replace").strip()
        db.log_error("camera", f"libcamera-still failed (exit {e.returncode}): {stderr_msg}")
        return None

    except Exception as e:
        db.log_error("camera", f"Unexpected capture error: {e}")
        return None


def _update_latest_symlink(filepath):
    """
    Updates config.LATEST_PHOTO to be a symlink pointing to filepath.
    Removes the old symlink first if it exists.

    A symlink (symbolic link) is a file that acts as a shortcut to another
    file. feh watches the symlink path — when we update the symlink to point
    to a new photo file, feh sees the "latest.jpg" file change and reloads.
    This is more reliable than renaming/overwriting the file directly.
    """
    try:
        if os.path.lexists(config.LATEST_PHOTO):
            os.remove(config.LATEST_PHOTO)   # Remove old symlink (not the file it pointed to)
        os.symlink(filepath, config.LATEST_PHOTO)
    except Exception as e:
        db.log_error("camera", f"Failed to update latest.jpg symlink: {e}", level="WARNING")


def _is_night():
    """
    Returns True if the current hour is within the night window.
    Determines which photo interval to use (day vs night).
    """
    h = datetime.datetime.now().hour
    return h >= config.NIGHT_START_HOUR or h < config.NIGHT_END_HOUR


def get_latest_photo_info():
    """
    Returns information about the most recently captured photo.
    Used by dashboard.py to display the "Last photo" timestamp.

    Returns:
        tuple: (filepath: str, timestamp_str: str) if a photo exists
        tuple: (None, "No photos yet") if no photo has been taken yet
    """
    try:
        if os.path.exists(config.LATEST_PHOTO):
            real_path = os.path.realpath(config.LATEST_PHOTO)   # Resolve symlink
            mtime     = os.path.getmtime(real_path)
            ts        = datetime.datetime.fromtimestamp(mtime).strftime("%b %d  %H:%M")
            return real_path, ts
    except Exception:
        pass
    return None, "No photos yet"


def camera_loop():
    """
    The main camera scheduling loop — runs forever in a background daemon thread.

    Waits for the configured stagger delay before starting (CAMERA_START_DELAY)
    so the camera doesn't compete with the sensor initialisation burst on startup.

    Alternates between day and night photo intervals based on current time:
      - Daytime  (PHOTO_INTERVAL_DAY):   every 15 minutes = 96 photos/day
      - Nighttime (PHOTO_INTERVAL_NIGHT): every 60 minutes = 8 photos/night
      - Total: roughly 104 photos per day at ~200KB each = ~20MB/day on the USB drive

    Labels photos "day" or "night" so you can filter by time-of-day in the
    photos folder without reading the timestamp on each file.
    """
    # Stagger delay — camera starts after sensors and dashboard are already running
    time.sleep(config.CAMERA_START_DELAY)

    while True:
        night    = _is_night()
        interval = config.PHOTO_INTERVAL_NIGHT if night else config.PHOTO_INTERVAL_DAY
        label    = "night" if night else "day"

        capture_photo(label=label)

        # Sleep until next photo is due
        time.sleep(interval)

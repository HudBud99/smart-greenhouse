#!/bin/bash
# =============================================================================
# SETUP.sh — FULL INSTALLATION GUIDE
# =============================================================================
# Run these commands in order after flashing the Pi and connecting via SSH.
# Read each section before running — some steps require decisions from you.
#
# RECOMMENDED OS (via Raspberry Pi Imager):
#   Raspberry Pi OS Lite (64-bit) — NO desktop pre-installed
#   Why Lite? It uses ~200MB less RAM than the Full version, leaving more
#   memory for the sensor loops, dashboard, and database operations.
#   The minimal desktop (lxde-core) is installed below — just what's needed.
#
# ENABLE THESE IN IMAGER BEFORE FLASHING (click the gear icon):
#   ✓ Set hostname: greenhouse
#   ✓ Enable SSH
#   ✓ Set username: pi
#   ✓ Set your WiFi SSID and password
#   ✓ Set your timezone and locale
# =============================================================================


# =============================================================================
# STEP 1 — SYSTEM UPDATE
# =============================================================================
# Always update before installing anything on a fresh Pi OS image.

sudo apt update && sudo apt upgrade -y


# =============================================================================
# STEP 2 — ENABLE SPI AND I2C INTERFACES
# =============================================================================
# SPI  → needed for MCP3008 soil sensor ADCs
# I2C  → needed for MCP23017 relay GPIO expander
# Both are DISABLED by default in Raspberry Pi OS Lite.
# Run raspi-config and enable both before running any Python code.

sudo raspi-config
# Navigate: Interface Options → SPI → Enable
# Navigate: Interface Options → I2C → Enable
# Then reboot when prompted, or reboot manually after closing raspi-config.

# VERIFY they're enabled after reboot:
# ls /dev/spidev*    → should show /dev/spidev0.0  /dev/spidev0.1
# ls /dev/i2c*       → should show /dev/i2c-1
# i2cdetect -y 1     → should show 0x20 (your MCP23017)


# =============================================================================
# STEP 3 — INSTALL PYTHON LIBRARIES
# =============================================================================
# All libraries needed to run the greenhouse controller.

sudo apt install -y python3-pip python3-libcamera

# CircuitPython hardware libraries (Adafruit)
pip3 install --break-system-packages \
    adafruit-circuitpython-mcp3xxx \
    adafruit-circuitpython-dht \
    adafruit-circuitpython-mcp230xx \
    RPi.GPIO \
    rich

# Verify the MCP23017 is detected on the I2C bus at address 0x20
# Run this AFTER wiring the MCP23017 with A0/A1/A2 tied to GND:
sudo apt install -y i2c-tools
i2cdetect -y 1
# You should see a "20" in the output grid


# =============================================================================
# STEP 4 — INSTALL MINIMAL DESKTOP (for TV display + feh)
# =============================================================================
# The dashboard runs in a terminal and feh displays photos in a window.
# Both need a display server. lxde-core is the lightest option available
# — it uses about 50–80MB RAM vs 300MB+ for the full LXDE desktop.

sudo apt install -y lxde-core feh xterm

# Set the Pi to boot to desktop (needed to show the TV display on startup)
sudo raspi-config
# Navigate: System Options → Boot / Auto Login → Desktop Autologin


# =============================================================================
# STEP 5 — MOUNT USB JUMP DRIVE (for photo and data storage)
# =============================================================================
# The jump drive stores photos to avoid wearing out the SD card.
# These steps make it auto-mount at /mnt/usb every time the Pi boots.

# First, plug in your USB drive and find its UUID:
sudo blkid
# Look for the line showing your USB drive (usually /dev/sda1)
# Copy the UUID value — it looks like: UUID="ABCD-1234"

# Create the mount point directory:
sudo mkdir -p /mnt/usb

# Add auto-mount to /etc/fstab (replace UUID with yours from blkid):
echo 'UUID=YOUR-UUID-HERE  /mnt/usb  vfat  defaults,noatime,uid=1000,gid=1000  0  2' | sudo tee -a /etc/fstab

# Test the mount without rebooting:
sudo mount -a

# Create the photos directory on the drive:
mkdir -p /mnt/usb/photos

# Verify it's mounted and writable:
touch /mnt/usb/photos/test.txt && echo "Mount working!" && rm /mnt/usb/photos/test.txt


# =============================================================================
# STEP 6 — COPY GREENHOUSE CODE TO PI
# =============================================================================
# From your computer (replace with your Pi's IP address or hostname):
# scp -r greenhouse_final/ pi@greenhouse.local:/home/pi/greenhouse/

# Or clone from wherever you store the code:
# git clone https://github.com/your-repo/greenhouse.git /home/pi/greenhouse


# =============================================================================
# STEP 7 — SET UP feh TO AUTOSTART WITH THE DESKTOP
# =============================================================================
# feh watches latest.jpg and reloads it every 60 seconds.
# --reload 60    : check for new image every 60 seconds
# --scale-down   : shrink image to fit window if larger than screen
# --auto-zoom    : zoom to fill window
# Adding & runs it in the background so the autostart continues

mkdir -p ~/.config/lxsession/LXDE-pi/

# Add feh to desktop autostart:
echo '@feh --reload 60 --scale-down --auto-zoom /mnt/usb/photos/latest.jpg' \
    >> ~/.config/lxsession/LXDE-pi/autostart

# Also add the greenhouse terminal dashboard to autostart
# (opens in a full-screen xterm so it fills most of the TV):
echo '@xterm -fullscreen -e "cd /home/pi/greenhouse && python3 main.py"' \
    >> ~/.config/lxsession/LXDE-pi/autostart


# =============================================================================
# STEP 8 — INSTALL AS A SYSTEMD SERVICE (auto-start + auto-restart)
# =============================================================================
# The systemd service ensures greenhouse.py restarts automatically if it
# crashes, and starts automatically every time the Pi powers on.

sudo cp /home/pi/greenhouse/greenhouse.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable greenhouse    # Start on boot
sudo systemctl start greenhouse     # Start right now

# Check it's running:
sudo systemctl status greenhouse

# View live log output (press Ctrl+C to stop watching):
sudo journalctl -u greenhouse -f

# Restart after making code changes:
sudo systemctl restart greenhouse


# =============================================================================
# STEP 9 — VERIFY EVERYTHING IS WORKING
# =============================================================================

# Check SPI is working (should show two devices):
ls /dev/spidev*

# Check I2C is working (should show 0x20 in the grid):
i2cdetect -y 1

# Check USB drive is mounted:
df -h | grep usb

# Check the database was created and is being written to:
sqlite3 /home/pi/greenhouse/greenhouse.db "SELECT COUNT(*) FROM sensor_readings;"

# Check recent watering events:
sqlite3 /home/pi/greenhouse/greenhouse.db \
    "SELECT timestamp, zone_label, duration_seconds FROM watering_events ORDER BY id DESC LIMIT 5;"

# Export data to Excel (run any time):
cd /home/pi/greenhouse && python3 export_excel.py

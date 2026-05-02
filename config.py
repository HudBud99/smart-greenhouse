# =============================================================================
# config.py — CENTRAL CONFIGURATION FILE
# =============================================================================
# This is the ONLY file you should need to edit for basic setup and tuning.
# Every pin number, threshold, timing value, and path lives here.
# Nothing is hardcoded anywhere else — all other files import from this one.
#
# HARDWARE OVERVIEW:
#   - Raspberry Pi 3B (the brain)
#   - 2x MCP3008  (SPI analog-to-digital converters for 16 soil sensors)
#   - 1x MCP23017 (I2C GPIO expander — controls 16 relay outputs)
#   - 2x 8-channel relay boards (connected to MCP23017, switch solenoids)
#   - 1x 4-channel relay board (fan + 3 spare outputs, direct GPIO)
#   - 16x capacitive soil moisture sensors (3.3V, analog output)
#   - 16x 12V normally-closed solenoids (water valves on pressurized PVC)
#   - 1x 12V diaphragm pump with auto pressure switch (always pressurized)
#   - 2x DHT22 temperature/humidity sensors (inside + outside greenhouse)
#   - 1x Raspberry Pi Camera (photos saved to USB drive)
#   - 1x USB jump drive mounted at /mnt/usb (photo + data storage)
#   - 1x TV connected via HDMI (displays Rich terminal dashboard)
#
# WIRING NUMBERING SYSTEM:
#   All pin numbers below use BCM (Broadcom) numbering — this is what
#   RPi.GPIO and CircuitPython use when you call GPIO.setmode(GPIO.BCM).
#   Do NOT use the WiringPi numbers from the Pi4J/pi4j.com diagrams.
#   BCM numbers are the ones labeled "GPIO X" in the official Pi docs.
#
# BCM → PHYSICAL PIN QUICK REFERENCE (verified from gpio readall on Pi 3B):
#   BCM2  = Physical 3   BCM3  = Physical 5   BCM4  = Physical 7
#   BCM7  = Physical 26  BCM8  = Physical 24  BCM9  = Physical 21
#   BCM10 = Physical 19  BCM11 = Physical 23  BCM17 = Physical 11
#   BCM22 = Physical 15  BCM23 = Physical 16  BCM24 = Physical 18
#   BCM25 = Physical 22  BCM27 = Physical 13  BCM16 = Physical 36
# =============================================================================


# -----------------------------------------------------------------------------
# SPI — MCP3008 CHIP SELECT PINS
# -----------------------------------------------------------------------------
# The two MCP3008 chips share the same SPI bus (MOSI/MISO/SCLK) but each
# needs its own Chip Select (CS) pin so the Pi can talk to them one at a time.
# Hardware SPI CE0 and CE1 are the dedicated CS pins for the Pi's SPI bus.
#
#   MCP3008 #1 → BCM8 (CE0, Physical Pin 24) → reads soil zones 0–7
#   MCP3008 #2 → BCM7 (CE1, Physical Pin 26) → reads soil zones 8–15
#
# All 8 channels on each chip connect to one capacitive soil sensor each.
# Sensor VCC → Pi 3.3V   Sensor GND → Common ground bus bar
# -----------------------------------------------------------------------------
MCP3008_CS_PINS = [8, 7]   # [CE0 for chip #1, CE1 for chip #2]


# -----------------------------------------------------------------------------
# I2C — MCP23017 GPIO EXPANDER
# -----------------------------------------------------------------------------
# The MCP23017 gives us 16 extra GPIO output pins over just 2 wires (I2C).
# This lets us control both 8-channel relay boards without using up 16 GPIO
# pins directly on the Pi.
#
# I2C wiring:
#   SDA → BCM2 (Physical Pin 3)
#   SCL → BCM3 (Physical Pin 5)
#   VCC → Pi 3.3V
#   GND → Common ground bus bar
#   A0, A1, A2 → all tied to GND (sets I2C address to 0x20)
#
# The MCP23017 has two 8-pin ports:
#   Port A (GPA0–GPA7) → connects to Relay Board 1, IN1–IN8 → Zones 0–7
#   Port B (GPB0–GPB7) → connects to Relay Board 2, IN1–IN8 → Zones 8–15
#
# Relay board wiring (IMPORTANT — optocoupler isolation):
#   - Remove the JD-VCC jumper on each relay board (enables isolation)
#   - VCC pin  → Pi 3.3V (powers the optocoupler logic side only)
#   - JD-VCC   → 5V PSU positive (powers the relay coil side)
#   - GND      → Common ground bus bar (both PSU grounds tied together)
#   - IN1–IN8  → MCP23017 Port A or B pins
#   - COM      → 12V PSU positive
#   - NO       → Solenoid positive (normally open contact)
#   - Solenoid negative → 12V PSU negative → common ground bus bar
#
# Solenoids are NORMALLY CLOSED:
#   - No power = valve CLOSED (safe default, no flooding on Pi crash)
#   - Power applied = valve OPEN (water flows)
#   - Relay active-LOW: GPIO LOW = relay ON = solenoid energized = water flows
#   - Relay active-HIGH: GPIO HIGH = relay OFF = solenoid de-energized = closed
# -----------------------------------------------------------------------------
MCP23017_I2C_ADDRESS = 0x20   # Default address when A0/A1/A2 all tied to GND


# -----------------------------------------------------------------------------
# DHT22 SENSOR PINS — Temperature and Humidity
# -----------------------------------------------------------------------------
# Two DHT22 sensors: one inside the greenhouse, one outside.
# Each needs only one data wire + power + ground.
# Add a 10kΩ pull-up resistor between DATA and 3.3V on each sensor.
# DHT22 has a mandatory minimum 2-second gap between reads — enforced in code.
#
#   Inside sensor  DATA → BCM17 (Physical Pin 11)
#   Outside sensor DATA → BCM27 (Physical Pin 13)
#   VCC → 3.3V    GND → Common ground bus bar
# -----------------------------------------------------------------------------
DHT_INSIDE_PIN  = 17   # BCM17, Physical Pin 11
DHT_OUTSIDE_PIN = 27   # BCM27, Physical Pin 13


# -----------------------------------------------------------------------------
# FAN RELAY PIN — Direct GPIO (not through MCP23017)
# -----------------------------------------------------------------------------
# The fan is controlled by one channel on the 4-channel relay board.
# This relay board connects directly to Pi GPIO (not through the MCP23017)
# because the fan is controlled by temperature logic, not irrigation logic.
#
# Fan wiring:
#   Relay IN  → BCM22 (Physical Pin 15)
#   Relay VCC → Pi 3.3V
#   Relay GND → Common ground bus bar
#   Relay COM → 12V PSU positive
#   Relay NO  → Fan positive
#   Fan negative → 12V PSU negative → common ground
#
# The remaining 3 channels on the 4-channel relay board are spare:
#   BCM23 (Physical 16), BCM24 (Physical 18), BCM25 (Physical 22)
#   Use these for grow lights, pump control, heater, etc. in the future.
# -----------------------------------------------------------------------------
FAN_RELAY_PIN   = 22   # BCM22, Physical Pin 15 — PLACEHOLDER, update if needed


# -----------------------------------------------------------------------------
# SOIL MOISTURE THRESHOLDS
# -----------------------------------------------------------------------------
# The MCP3008 returns a 10-bit value from 0 to 1023.
# Capacitive sensors work INVERSELY to resistive ones:
#   LOW raw value  = WET soil  (sensor capacitance is high, voltage is low)
#   HIGH raw value = DRY soil  (sensor capacitance is low, voltage is high)
#
# To convert raw to percentage we invert: pct = 100 - (raw / 1023 * 100)
# So a raw value of 400 = about 60.9% moisture.
#
# SOIL_DRY_THRESHOLD: if raw reading is ABOVE this number, soil is too dry
# and the zone will be queued for watering.
#
# HOW TO CALIBRATE:
#   1. Put sensor in dry air → note the raw value (should be close to 1023)
#   2. Put sensor in wet soil → note the raw value (should be much lower)
#   3. Set threshold somewhere between those two readings
#   A starting point of 400-500 is typical for most capacitive sensors.
# -----------------------------------------------------------------------------
SOIL_DRY_THRESHOLD = 400   # Raw ADC value — above this = water the zone


# -----------------------------------------------------------------------------
# FAN TEMPERATURE THRESHOLDS
# -----------------------------------------------------------------------------
# The fan turns ON when inside temperature reaches FAN_ON_TEMP_C.
# The fan turns OFF when inside temperature drops below FAN_OFF_TEMP_C.
#
# The gap between ON and OFF thresholds is called HYSTERESIS.
# This prevents the fan from rapidly cycling on and off when the temperature
# hovers right at the trigger point (e.g. 30.0°, 30.1°, 29.9°, 30.0°...).
# With hysteresis, once the fan turns on at 30°C it won't turn off until
# the temperature drops all the way to 27°C.
# -----------------------------------------------------------------------------
FAN_ON_TEMP_C  = 30.0   # Fan turns ON  when inside temp reaches this (°C)
FAN_OFF_TEMP_C = 27.0   # Fan turns OFF when inside temp drops below this (°C)


# -----------------------------------------------------------------------------
# WATERING BEHAVIOR
# -----------------------------------------------------------------------------
# WATER_DURATION_SECONDS: how long each solenoid stays open per watering event.
# The solenoid queue runs one zone at a time — this is how long each one runs
# before the next zone in the queue fires.
#
# NO_WATER_AT_NIGHT: if True, the threshold-triggered watering is suppressed
# during night hours. The scheduled 7AM morning cycle still runs regardless.
# This prevents watering cold soil at night which can stress plants.
# -----------------------------------------------------------------------------
WATER_DURATION_SECONDS = 30    # Seconds each solenoid stays open
NO_WATER_AT_NIGHT      = True  # Suppress threshold watering during night hours


# -----------------------------------------------------------------------------
# MORNING WATER CYCLE
# -----------------------------------------------------------------------------
# Every morning at MORNING_CYCLE_HOUR, ALL 16 zones are queued to water
# regardless of current soil moisture readings. This provides a consistent
# baseline watering and ensures every plant gets water at least once per day
# even if the soil sensors aren't triggering on their own.
#
# The morning cycle runs through the normal queue system, so zones fire
# one at a time with WATER_DURATION_SECONDS each.
# Total morning cycle time = 16 zones × (WATER_DURATION_SECONDS + 2s gap)
# At 30s per zone that's about 8.5 minutes total.
# -----------------------------------------------------------------------------
MORNING_CYCLE_HOUR = 7   # 24-hour format — 7 = 7:00 AM


# -----------------------------------------------------------------------------
# NIGHT WINDOW DEFINITION
# -----------------------------------------------------------------------------
# Night is defined as any hour >= NIGHT_START_HOUR OR < NIGHT_END_HOUR.
# During night hours:
#   - Sensor polling slows down (SENSOR_READ_INTERVAL_NIGHT)
#   - Photos are taken less frequently (PHOTO_INTERVAL_NIGHT)
#   - Threshold-triggered watering is suppressed (if NO_WATER_AT_NIGHT = True)
#   - The morning cycle still fires at MORNING_CYCLE_HOUR regardless
# -----------------------------------------------------------------------------
NIGHT_START_HOUR = 21   # 9:00 PM — night mode begins
NIGHT_END_HOUR   = 7    # 7:00 AM — night mode ends (same as morning cycle)


# -----------------------------------------------------------------------------
# TIMING — all values in seconds
# -----------------------------------------------------------------------------
# Stagger delays prevent all loops from firing at the same instant on startup,
# which would cause a CPU spike and potentially corrupt the first DB writes.
# The sensor loop starts first (delay 0) so data exists before the dashboard
# tries to display it, and the camera starts last to avoid colliding with
# the initial sensor read burst.
# -----------------------------------------------------------------------------
SENSOR_READ_INTERVAL_DAY   = 60    # Read all sensors every 60s during daytime
SENSOR_READ_INTERVAL_NIGHT = 300   # Slow to every 5 minutes at night

PHOTO_INTERVAL_DAY   = 900    # Take a photo every 15 minutes during the day
PHOTO_INTERVAL_NIGHT = 3600   # Take a photo every hour at night

DASHBOARD_REFRESH_INTERVAL = 5   # Redraw the TV dashboard every 5 seconds

# Startup stagger delays (seconds after launch before each loop begins)
SENSOR_START_DELAY    = 0   # Sensors start immediately on launch
DASHBOARD_START_DELAY = 3   # Dashboard waits 3s so DB has data to display
CAMERA_START_DELAY    = 7   # Camera waits 7s to avoid colliding with sensors


# -----------------------------------------------------------------------------
# FILE PATHS
# -----------------------------------------------------------------------------
# DB_PATH: SQLite database file — created automatically on first run.
#   All sensor readings, watering events, errors, and daily summaries are
#   stored here. Much more reliable than writing directly to Excel.
#
# PHOTO_DIR: Directory on the USB jump drive where photos are saved.
#   Using the USB drive instead of the SD card prevents SD card wear-out,
#   which is the #1 cause of Pi failures in long-running projects.
#
# LATEST_PHOTO: A symlink (shortcut) that always points to the most recently
#   captured photo. The feh image viewer watches this path with --reload
#   so it automatically displays each new photo without restarting.
#
# LOG_FILE: Plain text error log as a backup to the DB error table.
# -----------------------------------------------------------------------------
DB_PATH      = "/home/pi/greenhouse/greenhouse.db"
PHOTO_DIR    = "/mnt/usb/photos"
LATEST_PHOTO = "/mnt/usb/photos/latest.jpg"
LOG_FILE     = "/home/pi/greenhouse/errors.log"


# -----------------------------------------------------------------------------
# ZONE LABELS — Friendly names for each of the 16 watering zones
# -----------------------------------------------------------------------------
# These names appear on the dashboard and in the database.
# Edit these to match what each zone actually waters in your greenhouse.
# Zone index matches the MCP3008 channel: Zone 0 = chip #1 channel 0, etc.
# -----------------------------------------------------------------------------
ZONE_LABELS = [
    "Zone 0",   "Zone 1",   "Zone 2",   "Zone 3",
    "Zone 4",   "Zone 5",   "Zone 6",   "Zone 7",
    "Zone 8",   "Zone 9",   "Zone 10",  "Zone 11",
    "Zone 12",  "Zone 13",  "Zone 14",  "Zone 15",
]

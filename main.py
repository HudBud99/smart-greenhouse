# =============================================================================
# main.py — PROGRAM ENTRY POINT
# =============================================================================
# This is the file you run to start the entire greenhouse system.
# It initialises all hardware, launches all background threads, and then
# hands control to the dashboard which runs on the main thread.
#
# HOW TO RUN MANUALLY (for testing):
#   cd /home/pi/greenhouse
#   python3 main.py
#
# HOW IT RUNS IN PRODUCTION:
#   As a systemd service (see greenhouse.service) that auto-starts on boot
#   and auto-restarts within 10 seconds if the program crashes.
#   Enable with: sudo systemctl enable greenhouse
#
# THREAD ARCHITECTURE:
#   Main thread    → dashboard.run_dashboard() (blocks here, renders TV display)
#   Thread 1       → sensor_loop() — reads all 16 soil + 2 DHT22 sensors
#   Thread 2       → irrigation.watering_worker() — fires solenoids from queue
#   Thread 3       → camera.camera_loop() — takes scheduled photos
#   Thread 4       → daily_summary_loop() — writes DB summaries at midnight
#
#   All background threads are DAEMON threads. This means they automatically
#   stop when the main thread exits — no zombie threads left running.
#
# STARTUP SEQUENCE:
#   1. Initialise SQLite database (create tables if first run)
#   2. Configure MCP23017 relay outputs (all set HIGH = all solenoids closed)
#   3. Configure fan relay GPIO pin (set HIGH = fan off)
#   4. Initialise MCP3008 SPI channels for soil sensors
#   5. Initialise DHT22 sensor objects
#   6. Launch all four daemon threads
#   7. Launch dashboard on main thread (blocks forever)
#
# SHUTDOWN:
#   Ctrl+C or systemd SIGTERM → _shutdown() handler fires
#   → All relay pins set HIGH (all solenoids close)
#   → Fan relay set HIGH (fan stops)
#   → GPIO cleanup
#   → sys.exit(0)
# =============================================================================

import threading
import time
import datetime
import signal
import sys
import RPi.GPIO as GPIO

import config
import db
import sensors
import irrigation
import camera
import dashboard
import fan


# =============================================================================
# HELPER
# =============================================================================

def _is_night():
    """Returns True if current hour is within the configured night window."""
    h = datetime.datetime.now().hour
    return h >= config.NIGHT_START_HOUR or h < config.NIGHT_END_HOUR


# =============================================================================
# SENSOR LOOP — Thread 1
# =============================================================================

def sensor_loop():
    """
    Runs forever in a daemon thread. Reads all sensors on a timed interval
    and triggers watering for dry zones.

    SEQUENCE PER CYCLE:
      1. Sleep for the appropriate interval (day or night rate)
         Actually: sleep happens at END of cycle so first read fires immediately
      2. Read inside DHT22 (temp + humidity)
      3. Read outside DHT22 (temp + humidity)
      4. Update fan state based on inside temperature
      5. Read all 16 soil sensors
      6. For each zone:
           a. Log reading to database regardless of value (None = NULL in DB)
           b. Skip watering logic if read failed (None)
           c. Skip watering if nighttime suppression is enabled
           d. If soil is dry → call irrigation.request_water() to queue the zone

    FAIL-SAFE WATERING LOGIC:
      A zone is only queued for watering if:
        - The soil read succeeded (not None)
        - It is daytime OR NO_WATER_AT_NIGHT is False
        - The raw reading exceeds SOIL_DRY_THRESHOLD

      This means a failed sensor read NEVER causes a watering event.
      Better to miss a watering than to flood already-wet soil.
    """
    time.sleep(config.SENSOR_START_DELAY)   # Stagger delay (0s — starts immediately)

    while True:
        night    = _is_night()
        interval = config.SENSOR_READ_INTERVAL_NIGHT if night else config.SENSOR_READ_INTERVAL_DAY

        # --- Read environment sensors ---
        temp_in,  hum_in  = sensors.read_dht_inside()
        temp_out, hum_out = sensors.read_dht_outside()

        # Update fan relay based on current inside temperature
        fan.update_fan(temp_in)

        # --- Read all 16 soil sensors ---
        soil_readings = sensors.read_all_soil()

        # --- Process each zone ---
        for zone, moisture in enumerate(soil_readings):

            # Always log the reading, even if None (stored as NULL in DB)
            # This preserves the time-series record even during sensor failures
            db.log_reading(zone, moisture, temp_in, temp_out, hum_in, hum_out)

            # SAFETY: skip watering logic completely on failed reads
            if moisture is None:
                continue

            # NIGHT SUPPRESSION: don't trigger threshold watering at night
            # The scheduled morning cycle bypasses this check (see morning_cycle_loop)
            if night and config.NO_WATER_AT_NIGHT:
                continue

            # THRESHOLD CHECK: queue zone if soil is too dry
            # irrigation.request_water() handles the re-queue/reset-timer logic
            if moisture > config.SOIL_DRY_THRESHOLD:
                irrigation.request_water(zone, moisture)

        # Sleep until next cycle
        time.sleep(interval)


# =============================================================================
# MORNING CYCLE LOOP — Thread 4 (shares thread slot with daily summary)
# =============================================================================

def morning_cycle_loop():
    """
    Fires at 7:00 AM every day — queues ALL 16 zones for watering regardless
    of current soil moisture levels. This provides a guaranteed daily baseline
    watering for every plant in the greenhouse.

    Uses a date-based deduplication check so the 7AM cycle only fires ONCE
    per day even if the check loop catches the 7:00 hour multiple times.

    The zones are queued through the normal irrigation queue so they fire
    one at a time, in order (Zone 0 first, Zone 15 last). Total time for
    all 16 zones at 30s each + 2s gaps = ~8.5 minutes.

    trigger_soil_raw=None is passed to request_water() to indicate this is
    a scheduled cycle, not a dry-soil trigger. In the database, watering_events
    rows from the morning cycle will have NULL in trigger_soil_raw.
    """
    last_triggered_date = None

    while True:
        now = datetime.datetime.now()

        # Check if it's the right hour and we haven't already run today
        if (now.hour == config.MORNING_CYCLE_HOUR
                and now.minute == 0
                and now.date() != last_triggered_date):

            db.log_error(
                "scheduler",
                f"Morning cycle triggered at {now.strftime('%H:%M')} — queuing all 16 zones",
                level="WARNING"   # WARNING level so it shows on dashboard and in Excel
            )

            # Queue all 16 zones — irrigation worker handles them one at a time
            for zone in range(16):
                irrigation.request_water(zone, soil_raw=None)   # None = scheduled, not threshold

            last_triggered_date = now.date()   # Prevent double-trigger within the same minute

        time.sleep(30)   # Check every 30 seconds — tight enough to never miss 7:00


# =============================================================================
# DAILY SUMMARY LOOP — Thread 4 (same thread as morning cycle)
# =============================================================================

def daily_summary_loop():
    """
    Fires once at midnight (00:00:05) to aggregate the previous day's data
    into the daily_summary table.

    The 5-second offset past midnight avoids the exact boundary condition
    where a reading timestamped "2026-04-09 23:59:59" might not yet be
    committed when the summary query runs.

    This thread also handles the morning cycle — they run sequentially in
    the same thread since neither is time-critical to the second.
    """
    while True:
        # Calculate how long to sleep until next midnight + 5 seconds
        now           = datetime.datetime.now()
        next_midnight = (now + datetime.timedelta(days=1)).replace(
            hour=0, minute=0, second=5, microsecond=0
        )
        sleep_secs = (next_midnight - now).total_seconds()
        time.sleep(sleep_secs)

        try:
            db.write_daily_summary()
        except Exception as e:
            db.log_error("daily_summary", f"Failed to write daily summary: {e}")


# =============================================================================
# GRACEFUL SHUTDOWN HANDLER
# =============================================================================

def _shutdown(signum, frame):
    """
    Called when the program receives SIGTERM (systemd stop) or SIGINT (Ctrl+C).
    Ensures all solenoids close and the fan stops before the process exits.
    This prevents any solenoid from being stuck open if the Pi is rebooted
    or the service is stopped while a zone is being watered.
    """
    print("\n[Greenhouse] Shutdown signal received — closing all solenoids...")
    irrigation.cleanup_relays()   # Sets all 16 relay outputs HIGH (all closed)
    fan.cleanup_fan()             # Sets fan relay HIGH (fan off)
    GPIO.cleanup()                # Releases all GPIO resources cleanly
    print("[Greenhouse] All solenoids closed. Exiting.")
    sys.exit(0)


# Register shutdown handler for both Ctrl+C and systemd stop signal
signal.signal(signal.SIGINT,  _shutdown)
signal.signal(signal.SIGTERM, _shutdown)


# =============================================================================
# STARTUP
# =============================================================================

if __name__ == "__main__":

    # Use BCM numbering throughout — must be set before any GPIO.setup() calls
    # fan.py uses RPi.GPIO directly (not MCP23017) so we set mode here globally
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)

    print("[Greenhouse] Step 1/5 — Initialising database...")
    db.init_db()
    # Creates greenhouse.db and all four tables if this is the first run
    # Safe to call on every startup — uses CREATE TABLE IF NOT EXISTS

    print("[Greenhouse] Step 2/5 — Configuring relay outputs (MCP23017 over I2C)...")
    irrigation.setup_relays()
    # Connects to MCP23017 at I2C address 0x20
    # Configures all 16 pins as outputs, all set HIGH (all solenoids closed)

    print("[Greenhouse] Step 3/5 — Configuring fan relay (GPIO)...")
    fan.setup_fan()
    # Configures BCM22 as output, set HIGH (fan off)

    print("[Greenhouse] Step 4/5 — Initialising soil sensors (MCP3008 over SPI)...")
    sensors.setup_sensors()
    # Initialises SPI bus and both MCP3008 chips
    # Builds the 16-channel list for read_all_soil()

    print("[Greenhouse] Step 5/5 — Initialising DHT22 temperature sensors...")
    sensors.setup_dht()
    # Creates DHT22 sensor objects for inside and outside pins

    # ── Launch background daemon threads ──
    # Daemon threads automatically stop when the main thread exits
    threads = [
        threading.Thread(
            target=sensor_loop,
            name="SensorLoop",
            daemon=True
        ),
        threading.Thread(
            target=irrigation.watering_worker,
            name="IrrigationWorker",
            daemon=True
        ),
        threading.Thread(
            target=camera.camera_loop,
            name="CameraLoop",
            daemon=True
        ),
        threading.Thread(
            target=morning_cycle_loop,
            name="MorningCycle",
            daemon=True
        ),
        threading.Thread(
            target=daily_summary_loop,
            name="DailySummary",
            daemon=True
        ),
    ]

    for t in threads:
        t.start()
        print(f"[Greenhouse] Started thread: {t.name}")

    print("[Greenhouse] All threads running. Launching dashboard...")
    print("[Greenhouse] Press Ctrl+C to stop cleanly.\n")

    # ── Dashboard runs on main thread — blocks here until shutdown ──
    # All sensor/irrigation/camera work happens in the daemon threads above
    dashboard.run_dashboard()

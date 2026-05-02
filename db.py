# =============================================================================
# db.py — DATABASE LAYER
# =============================================================================
# This file handles ALL reading and writing to the SQLite database.
# No other file in the project touches the database directly — everything
# goes through the functions defined here. This keeps data logic in one place.
#
# WHY SQLITE INSTEAD OF EXCEL?
#   Writing directly to an Excel file from a long-running process is fragile.
#   If the Pi loses power mid-write, the .xlsx file can corrupt and you lose
#   all your data. SQLite uses atomic transactions — if power cuts out during
#   a write, it automatically rolls back to the last clean state. Your data
#   is always valid. You can export to Excel any time using export_excel.py.
#
# DATABASE FILE LOCATION:
#   Defined in config.py as DB_PATH. Created automatically on first run.
#   The file is a single .db file that contains all four tables below.
#
# THE FOUR TABLES:
#   1. sensor_readings  — one row per zone per sensor cycle (every 60s/300s)
#   2. watering_events  — one row every time a solenoid actually fires
#   3. error_log        — one row per error or warning from any module
#   4. daily_summary    — one row per zone per day (written at midnight)
#
# WHAT EACH COLUMN STORES IS DOCUMENTED INSIDE init_db() BELOW.
# =============================================================================

import sqlite3
import datetime
import config


# =============================================================================
# DATABASE INITIALISATION
# =============================================================================

def init_db():
    """
    Creates the database file and all four tables on first run.
    Uses CREATE TABLE IF NOT EXISTS so it is completely safe to call
    every time the program starts — it will never overwrite existing data.
    """
    with sqlite3.connect(config.DB_PATH) as con:
        con.executescript("""

            -- ================================================================
            -- TABLE 1: sensor_readings
            -- ================================================================
            -- Written once per sensor cycle for EACH of the 16 zones.
            -- With 16 zones and a 60-second day interval, this table grows
            -- by 16 rows per minute = ~23,000 rows per day. After 30 days
            -- that's ~690,000 rows — SQLite handles millions easily.
            --
            -- COLUMNS:
            --   id               Auto-incrementing unique row identifier
            --   timestamp        ISO-8601 date+time string, e.g. "2026-04-09T14:32:05"
            --   zone             Zone number 0–15 (matches ZONE_LABELS index)
            --   zone_label       Friendly name from config.ZONE_LABELS
            --   soil_raw         Raw MCP3008 reading 0–1023
            --                    Lower = wetter, Higher = drier
            --                    NULL if the sensor read failed
            --   soil_percent     Soil moisture as 0–100% (inverted from raw)
            --                    100% = fully wet, 0% = bone dry
            --                    NULL if the sensor read failed
            --   temp_inside_c    Inside greenhouse temperature in Celsius from DHT22
            --                    NULL if DHT22 read failed after all retries
            --   temp_outside_c   Outside temperature in Celsius from DHT22
            --                    NULL if DHT22 read failed after all retries
            --   humidity_inside  Inside relative humidity 0–100% from DHT22
            --   humidity_outside Outside relative humidity 0–100% from DHT22
            --   is_night         1 if this reading was taken during night hours
            --                    0 if taken during daytime
            --                    (Night defined by NIGHT_START_HOUR/NIGHT_END_HOUR)
            -- ================================================================
            CREATE TABLE IF NOT EXISTS sensor_readings (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp        TEXT    NOT NULL,
                zone             INTEGER NOT NULL,
                zone_label       TEXT,
                soil_raw         INTEGER,
                soil_percent     REAL,
                temp_inside_c    REAL,
                temp_outside_c   REAL,
                humidity_inside  REAL,
                humidity_outside REAL,
                is_night         INTEGER DEFAULT 0
            );

            -- ================================================================
            -- TABLE 2: watering_events
            -- ================================================================
            -- Written once every time a solenoid actually fires and completes.
            -- This is your full irrigation history. You can query this to see
            -- exactly when each zone was watered, why, and for how long.
            --
            -- NOTE: trigger_soil_raw will be NULL for the 7AM morning cycle
            -- because the morning cycle fires all zones regardless of soil
            -- moisture — there is no trigger reading. A NULL here = scheduled
            -- watering, not threshold-triggered watering.
            --
            -- COLUMNS:
            --   id                 Auto-incrementing unique row identifier
            --   timestamp          ISO-8601 date+time when watering STARTED
            --   zone               Zone number 0–15
            --   zone_label         Friendly name from config.ZONE_LABELS
            --   duration_seconds   How many seconds the solenoid stayed open
            --                      Should match WATER_DURATION_SECONDS in config
            --   trigger_soil_raw   The raw soil reading that caused this watering
            --                      NULL = triggered by morning cycle schedule
            --   trigger_soil_pct   Same as above converted to percentage
            --                      NULL = triggered by morning cycle schedule
            --   queue_wait_seconds How many seconds this zone waited in the queue
            --                      before the solenoid actually fired
            --                      (other zones ahead of it in the queue)
            -- ================================================================
            CREATE TABLE IF NOT EXISTS watering_events (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp          TEXT    NOT NULL,
                zone               INTEGER NOT NULL,
                zone_label         TEXT,
                duration_seconds   INTEGER,
                trigger_soil_raw   INTEGER,
                trigger_soil_pct   REAL,
                queue_wait_seconds REAL
            );

            -- ================================================================
            -- TABLE 3: error_log
            -- ================================================================
            -- Written whenever any module catches an exception or warning.
            -- Visible on the TV dashboard so you can see problems without
            -- needing to SSH into the Pi and tail a log file.
            --
            -- COLUMNS:
            --   id        Auto-incrementing unique row identifier
            --   timestamp ISO-8601 date+time when the error occurred
            --   source    Which module logged the error
            --             e.g. "sensors", "irrigation", "camera", "fan",
            --                  "scheduler", "daily_summary"
            --   level     Severity — either "WARNING" or "ERROR"
            --             WARNING = something went wrong but was recovered
            --             ERROR   = something failed completely
            --   message   Human-readable description of what went wrong
            --             Includes zone numbers, sensor pin, retry counts, etc.
            -- ================================================================
            CREATE TABLE IF NOT EXISTS error_log (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT    NOT NULL,
                source    TEXT,
                level     TEXT    DEFAULT 'ERROR',
                message   TEXT
            );

            -- ================================================================
            -- TABLE 4: daily_summary
            -- ================================================================
            -- Written once at midnight for each of the 16 zones.
            -- Aggregates the previous day's sensor_readings and watering_events
            -- into one summary row per zone per day.
            -- This is the table most useful for long-term trend analysis in
            -- Excel — much smaller than loading all raw sensor_readings.
            --
            -- COLUMNS:
            --   id                  Auto-incrementing unique row identifier
            --   date                Date string e.g. "2026-04-09" (yesterday)
            --   zone                Zone number 0–15
            --   zone_label          Friendly name from config.ZONE_LABELS
            --   avg_soil_pct        Average soil moisture % across the whole day
            --   min_soil_pct        Driest point of the day (lowest moisture %)
            --   max_soil_pct        Wettest point of the day (highest moisture %)
            --   times_watered       How many watering events fired for this zone
            --   total_water_seconds Total seconds of watering across the day
            --   avg_temp_inside     Average inside temperature for the day (°C)
            --   avg_temp_outside    Average outside temperature for the day (°C)
            -- ================================================================
            CREATE TABLE IF NOT EXISTS daily_summary (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                date                TEXT    NOT NULL,
                zone                INTEGER NOT NULL,
                zone_label          TEXT,
                avg_soil_pct        REAL,
                min_soil_pct        REAL,
                max_soil_pct        REAL,
                times_watered       INTEGER,
                total_water_seconds INTEGER,
                avg_temp_inside     REAL,
                avg_temp_outside    REAL
            );

        """)


# =============================================================================
# INTERNAL HELPER FUNCTIONS
# =============================================================================

def _now():
    """Returns the current date and time as an ISO-8601 string."""
    return datetime.datetime.now().isoformat(timespec="seconds")


def _is_night():
    """
    Returns 1 if the current hour falls within the configured night window,
    0 if it is daytime. Used to tag sensor readings with the is_night flag.
    """
    h = datetime.datetime.now().hour
    return 1 if (h >= config.NIGHT_START_HOUR or h < config.NIGHT_END_HOUR) else 0


def _raw_to_pct(raw):
    """
    Converts a raw MCP3008 reading (0–1023) to a moisture percentage (0–100%).
    The conversion is INVERTED because capacitive sensors output lower voltage
    in wet soil (lower raw value = more moisture).
    Returns None if the raw value is None (failed read).
    """
    if raw is None:
        return None
    return round(100.0 - (raw / 1023.0 * 100.0), 1)


# =============================================================================
# WRITE FUNCTIONS — called by sensor loop, irrigation worker, etc.
# =============================================================================

def log_reading(zone, soil_raw, temp_in, temp_out, hum_in, hum_out):
    """
    Writes one row to sensor_readings for a single zone.
    Called 16 times per sensor cycle (once per zone).
    soil_raw can be None if the MCP3008 read failed — stored as NULL in DB.
    temp_in, temp_out, hum_in, hum_out can be None if DHT22 read failed.
    """
    soil_pct = _raw_to_pct(soil_raw)
    label    = config.ZONE_LABELS[zone]

    with sqlite3.connect(config.DB_PATH) as con:
        con.execute("""
            INSERT INTO sensor_readings
                (timestamp, zone, zone_label, soil_raw, soil_percent,
                 temp_inside_c, temp_outside_c, humidity_inside,
                 humidity_outside, is_night)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (_now(), zone, label, soil_raw, soil_pct,
              temp_in, temp_out, hum_in, hum_out, _is_night()))


def log_watering(zone, duration, trigger_soil_raw, queue_wait):
    """
    Writes one row to watering_events when a solenoid finishes firing.
    Called from the irrigation worker thread after each zone completes.

    trigger_soil_raw is None for morning cycle events (no threshold trigger).
    queue_wait is how many seconds the zone sat in queue before firing.
    """
    with sqlite3.connect(config.DB_PATH) as con:
        con.execute("""
            INSERT INTO watering_events
                (timestamp, zone, zone_label, duration_seconds,
                 trigger_soil_raw, trigger_soil_pct, queue_wait_seconds)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (_now(), zone, config.ZONE_LABELS[zone], duration,
              trigger_soil_raw, _raw_to_pct(trigger_soil_raw),
              round(queue_wait, 1)))


def log_error(source, message, level="ERROR"):
    """
    Writes one row to error_log.
    Called from any module that catches an exception.
    source = module name string, e.g. "sensors" or "camera"
    level  = "WARNING" for recovered errors, "ERROR" for failures
    """
    with sqlite3.connect(config.DB_PATH) as con:
        con.execute("""
            INSERT INTO error_log (timestamp, source, level, message)
            VALUES (?, ?, ?, ?)
        """, (_now(), source, level, message))


def write_daily_summary():
    """
    Aggregates yesterday's data from sensor_readings and watering_events
    into one row per zone in the daily_summary table.

    Called automatically at midnight from the daily_summary_loop in main.py.
    Uses SQL AVG/MIN/MAX/COUNT/SUM aggregate functions for efficiency —
    SQLite does all the math, no need to load data into Python first.

    If a zone had no sensor readings yesterday (e.g. sensor was disconnected),
    no summary row is written for that zone that day.
    """
    yesterday = (datetime.datetime.now() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")

    with sqlite3.connect(config.DB_PATH) as con:
        for zone in range(16):
            label = config.ZONE_LABELS[zone]

            # Get soil and temperature averages from sensor_readings
            soil = con.execute("""
                SELECT AVG(soil_percent), MIN(soil_percent), MAX(soil_percent),
                       AVG(temp_inside_c), AVG(temp_outside_c)
                FROM sensor_readings
                WHERE date(timestamp) = ? AND zone = ?
            """, (yesterday, zone)).fetchone()

            # Get watering count and total duration from watering_events
            water = con.execute("""
                SELECT COUNT(*), COALESCE(SUM(duration_seconds), 0)
                FROM watering_events
                WHERE date(timestamp) = ? AND zone = ?
            """, (yesterday, zone)).fetchone()

            # Only write a summary if there were actual sensor readings
            if soil and soil[0] is not None:
                con.execute("""
                    INSERT INTO daily_summary
                        (date, zone, zone_label, avg_soil_pct, min_soil_pct,
                         max_soil_pct, times_watered, total_water_seconds,
                         avg_temp_inside, avg_temp_outside)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (yesterday, zone, label,
                      round(soil[0], 1), round(soil[1], 1), round(soil[2], 1),
                      water[0], water[1],
                      round(soil[3], 1) if soil[3] else None,
                      round(soil[4], 1) if soil[4] else None))


# =============================================================================
# READ FUNCTIONS — called by dashboard.py to populate the TV display
# =============================================================================

def get_latest_readings():
    """
    Returns the most recent sensor reading for each of the 16 zones.
    Uses a subquery to find the highest row ID per zone (= most recent).
    Returns a dictionary keyed by zone number: { 0: {...}, 1: {...}, ... }
    Used by the dashboard to display current moisture levels and temperatures.
    """
    with sqlite3.connect(config.DB_PATH) as con:
        con.row_factory = sqlite3.Row   # Makes rows accessible as dictionaries
        rows = con.execute("""
            SELECT * FROM sensor_readings
            WHERE id IN (
                SELECT MAX(id) FROM sensor_readings GROUP BY zone
            )
            ORDER BY zone
        """).fetchall()
    return {row["zone"]: dict(row) for row in rows}


def get_last_watered(zone):
    """
    Returns the timestamp string of the most recent watering event for a zone.
    Returns None if the zone has never been watered.
    Used by the dashboard to show "Last Watered" column.
    """
    with sqlite3.connect(config.DB_PATH) as con:
        row = con.execute("""
            SELECT timestamp FROM watering_events
            WHERE zone = ? ORDER BY id DESC LIMIT 1
        """, (zone,)).fetchone()
    return row[0] if row else None


def get_recent_errors(limit=5):
    """
    Returns the most recent error_log rows, newest first.
    Default limit of 5 keeps the dashboard error panel compact.
    Used by dashboard.py to show the error panel at the bottom of the screen.
    """
    with sqlite3.connect(config.DB_PATH) as con:
        con.row_factory = sqlite3.Row
        return con.execute("""
            SELECT * FROM error_log ORDER BY id DESC LIMIT ?
        """, (limit,)).fetchall()

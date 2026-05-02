# =============================================================================
# export_excel.py — EXPORT DATABASE TO EXCEL
# =============================================================================
# Run this manually whenever you want to analyse greenhouse data in Excel.
# It exports all four database tables into one .xlsx file with four sheets.
#
# HOW TO RUN:
#   cd /home/pi/greenhouse
#   python3 export_excel.py
#
# OUTPUT:
#   Creates a file like: greenhouse_export_20260409_1432.xlsx
#   in the current directory (same folder as this script).
#
# THE FOUR EXCEL SHEETS:
#   1. "Daily Summary"   — best for trend analysis (one row per zone per day)
#   2. "Watering Events" — full irrigation history (every solenoid fire)
#   3. "Sensor Readings" — full raw sensor data (every 60s reading)
#   4. "Error Log"       — last 500 errors and warnings
#
# WHY NOT WRITE DIRECTLY TO EXCEL?
#   Writing to .xlsx files from a running Python process is fragile and slow.
#   SQLite is the live data store — this script is just an export tool.
#   Run it any time you want, as many times as you want — it never modifies
#   the database, only reads from it.
#
# DEPENDENCIES:
#   pandas and openpyxl are installed automatically if not present.
#   If pip is unavailable, run manually first:
#     pip3 install pandas openpyxl --break-system-packages
# =============================================================================

import sqlite3
import datetime
import sys
import subprocess

import config


def _ensure_dependencies():
    """
    Installs pandas and openpyxl if they're not already available.
    pandas handles the DB → DataFrame conversion.
    openpyxl is the Excel engine pandas uses to write .xlsx files.
    """
    try:
        import pandas
        import openpyxl
    except ImportError:
        print("Installing required packages (pandas, openpyxl)...")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "pandas", "openpyxl",
             "--break-system-packages"],
            check=True
        )
        print("Packages installed successfully.\n")


def export():
    """
    Reads all four tables from the SQLite database and writes them to
    a date-stamped Excel file with one sheet per table.

    Newest rows are shown first in each sheet (ORDER BY id DESC) so the
    most recent data is at the top when you open the file.

    The sensor_readings table can get very large (16 zones × every 60s =
    ~23,000 rows/day). If you only want recent data, change the SQL query
    in the pandas.read_sql_query call to add a WHERE clause, e.g.:
        WHERE date(timestamp) >= date('now', '-30 days')
    """
    _ensure_dependencies()
    import pandas as pd

    date_str = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    filename = f"greenhouse_export_{date_str}.xlsx"

    print(f"Connecting to database: {config.DB_PATH}")
    con = sqlite3.connect(config.DB_PATH)

    print("Reading tables...")

    # Daily summary — one row per zone per day — best for trend charts
    summary_df = pd.read_sql_query(
        "SELECT * FROM daily_summary ORDER BY date DESC, zone ASC",
        con
    )

    # Watering events — full irrigation history
    water_df = pd.read_sql_query(
        "SELECT * FROM watering_events ORDER BY id DESC",
        con
    )

    # Raw sensor readings — every individual sensor cycle
    # This can be millions of rows after months of operation
    sensor_df = pd.read_sql_query(
        "SELECT * FROM sensor_readings ORDER BY id DESC",
        con
    )

    # Error log — capped at 500 most recent entries to keep file size manageable
    error_df = pd.read_sql_query(
        "SELECT * FROM error_log ORDER BY id DESC LIMIT 500",
        con
    )

    con.close()

    print(f"Writing to {filename}...")

    # Write all four dataframes to separate sheets in one .xlsx file
    with pd.ExcelWriter(filename, engine="openpyxl") as writer:
        summary_df.to_excel(writer, sheet_name="Daily Summary",    index=False)
        water_df.to_excel(  writer, sheet_name="Watering Events",  index=False)
        sensor_df.to_excel( writer, sheet_name="Sensor Readings",  index=False)
        error_df.to_excel(  writer, sheet_name="Error Log",        index=False)

    # Print summary of what was exported
    print(f"\n✓ Export complete: {filename}")
    print(f"  Daily Summary rows:    {len(summary_df):,}")
    print(f"  Watering Event rows:   {len(water_df):,}")
    print(f"  Sensor Reading rows:   {len(sensor_df):,}")
    print(f"  Error Log rows:        {len(error_df):,}")
    print(f"\nCopy the file to a USB drive or transfer via SCP:")
    print(f"  scp pi@greenhouse.local:/home/pi/greenhouse/{filename} ~/Desktop/")


if __name__ == "__main__":
    export()

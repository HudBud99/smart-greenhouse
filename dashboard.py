# =============================================================================
# dashboard.py — TV DISPLAY USING RICH TERMINAL LIBRARY
# =============================================================================
# Renders a live dashboard on the TV connected to the Pi via HDMI.
# Uses the Rich library for coloured terminal output — no web server needed,
# no browser, very lightweight (a few MB RAM vs 200MB+ for a browser).
#
# WHAT'S DISPLAYED:
#   Top section (70% of screen):
#     - All 16 soil moisture zones with colour-coded moisture bars
#     - Raw ADC value, moisture %, current status, last watered time
#   Bottom section (30% of screen), three panels side by side:
#     - Environment: inside/outside temp, humidity, fan status
#     - System Status: active watering zone, queue, morning cycle info, photo
#     - Recent Errors: last 5 errors from the database
#
# COLOUR CODING FOR MOISTURE BARS:
#   Cyan  (█) = 70–100% moisture — very wet
#   Green (█) = 45–69%  moisture — healthy range
#   Yellow(█) = 25–44%  moisture — getting dry
#   Red   (█) = 0–24%   moisture — critically dry, needs water
#
# PERFORMANCE:
#   The dashboard reads from the SQLite database on each refresh cycle.
#   SQLite reads are extremely fast (microseconds) for small queries.
#   The 5-second refresh interval (DASHBOARD_REFRESH_INTERVAL) means the
#   Pi is doing one DB read burst every 5 seconds, which is negligible.
#   All the heavy work (sensors, irrigation, camera) runs in other threads.
#
# RUNS ON MAIN THREAD:
#   dashboard.run_dashboard() blocks forever and runs on the main thread.
#   All sensors, irrigation, camera, and daily summary run in daemon threads.
#   If main thread exits, daemon threads automatically stop — clean shutdown.
# =============================================================================

import time
import datetime
from rich.console import Console
from rich.table import Table
from rich.layout import Layout
from rich.panel import Panel
from rich.live import Live
from rich.text import Text
from rich import box

import config
import db
import irrigation
import camera
import fan

# Rich console object — used for rendering to the terminal
console = Console()


# =============================================================================
# COLOUR AND BAR HELPERS
# =============================================================================

def _soil_colour(pct):
    """
    Returns a Rich colour string based on soil moisture percentage.
    Used to colour both the ASCII bar and the text in the zone table.

    pct: float 0–100 (moisture percentage) or None (failed read)
    """
    if pct is None:
        return "dim white"      # Grey = unknown/no reading
    if pct >= 70:
        return "bright_cyan"    # Cyan = very wet
    if pct >= 45:
        return "bright_green"   # Green = healthy
    if pct >= 25:
        return "yellow"         # Yellow = getting dry
    return "bright_red"         # Red = critically dry


def _soil_bar(pct, width=10):
    """
    Returns an ASCII progress bar string representing moisture level.
    █ = filled (wet), ░ = empty (dry)
    Example: "████████░░  82.3%"

    pct: float 0–100 or None
    width: total number of bar characters (default 10)
    """
    if pct is None:
        return "─" * width + "  ???%"
    filled = int((pct / 100.0) * width)
    bar    = "█" * filled + "░" * (width - filled)
    return f"{bar}  {pct:>5.1f}%"


# =============================================================================
# PANEL BUILDERS — each returns a Rich renderable object
# =============================================================================

def _build_zone_table(readings, last_watered, active_zone, queue):
    """
    Builds the main 16-zone soil moisture table for the top of the screen.

    readings:     dict from db.get_latest_readings() — {zone: row_dict}
    last_watered: dict {zone: timestamp_str or None}
    active_zone:  int or None — zone currently being watered
    queue:        list of zone numbers waiting in queue

    Status badges (rightmost column):
      💧 WATERING = this zone's solenoid is currently open (blinking in terminal)
      ⏳ Queue #N = this zone is waiting in the queue at position N
      🔴 DRY     = soil is below threshold (but nighttime suppression or still queuing)
      ⚠ NO READ  = sensor read failed this cycle
      ✓ OK       = soil moisture is fine, no action needed
    """
    table = Table(
        title="🌱  Soil Moisture — All Zones",
        box=box.SIMPLE_HEAVY,
        expand=True,
        style="on grey11",   # Dark background for the table
    )

    # Define columns with fixed widths to prevent layout jumping on refresh
    table.add_column("Zone",         style="bold white", width=14)
    table.add_column("Moisture",     width=22)
    table.add_column("Raw",          style="dim white",  width=6,  justify="right")
    table.add_column("Status",       width=14,           justify="center")
    table.add_column("Last Watered", style="dim white",  width=20)

    # Calculate the dry threshold as a percentage for status badge comparison
    dry_pct = 100.0 - (config.SOIL_DRY_THRESHOLD / 1023.0 * 100.0)

    for zone in range(16):
        label = config.ZONE_LABELS[zone]
        data  = readings.get(zone)

        # Extract values from latest reading, default to None if no data yet
        raw = data.get("soil_raw")      if data else None
        pct = data.get("soil_percent")  if data else None

        # Build moisture bar and determine colour
        colour  = _soil_colour(pct)
        bar_str = _soil_bar(pct)
        raw_str = str(raw) if raw is not None else "—"

        # Determine status badge for this zone
        if zone == active_zone:
            status = Text("💧 WATERING", style="bold bright_cyan")
        elif zone in queue:
            pos    = queue.index(zone) + 1
            status = Text(f"⏳ Queue #{pos}", style="yellow")
        elif pct is None:
            status = Text("⚠ NO READ", style="dim red")
        elif pct < dry_pct:
            status = Text("🔴 DRY", style="bold red")
        else:
            status = Text("✓ OK", style="green")

        # Format last watered timestamp — ISO string to human-readable
        lw = last_watered.get(zone) or "Never"
        if lw and lw != "Never":
            try:
                dt = datetime.datetime.fromisoformat(lw)
                lw = dt.strftime("%b %d  %H:%M")   # e.g. "Apr 09  14:32"
            except Exception:
                pass   # If formatting fails, show raw string

        table.add_row(
            label,
            Text(bar_str, style=colour),
            raw_str,
            status,
            lw,
        )

    return table


def _build_environment_panel(readings):
    """
    Builds the environment panel showing temperatures, humidity, and fan status.
    Pulls temp/humidity from any zone's latest reading (all zones share the same
    DHT22 values since there's only one inside and one outside sensor).

    Temperature colour coding:
      Red    = > 35°C (dangerously hot)
      Yellow = > 28°C (warm, fan should be on)
      Green  = > 15°C (ideal greenhouse range)
      Cyan   = ≤ 15°C (cold)
    """
    # Find the first zone that has valid temperature data
    temp_in = temp_out = hum_in = hum_out = None
    for data in readings.values():
        if data.get("temp_inside_c") is not None:
            temp_in  = data["temp_inside_c"]
            temp_out = data["temp_outside_c"]
            hum_in   = data["humidity_inside"]
            hum_out  = data["humidity_outside"]
            break

    def _fmt(val, unit=""):
        """Formats a value with unit, or returns '—' if None."""
        return f"{val}{unit}" if val is not None else "—"

    def _temp_colour(c):
        """Returns colour string based on temperature."""
        if c is None: return "white"
        if c > 35:    return "bright_red"
        if c > 28:    return "yellow"
        if c > 15:    return "bright_green"
        return "bright_cyan"

    fan_status = "▶ RUNNING" if fan.is_fan_on() else "■ OFF"
    fan_colour = "bright_cyan" if fan.is_fan_on() else "dim white"

    lines = [
        f"[bold]Inside temp[/bold]    [{_temp_colour(temp_in)}]{_fmt(temp_in, '°C')}[/]",
        f"[bold]Outside temp[/bold]   [{_temp_colour(temp_out)}]{_fmt(temp_out, '°C')}[/]",
        f"[bold]Inside hum[/bold]     [cyan]{_fmt(hum_in, '%')}[/]",
        f"[bold]Outside hum[/bold]    [cyan]{_fmt(hum_out, '%')}[/]",
        "",
        f"[bold]Fan trigger[/bold]    [dim]≥ {config.FAN_ON_TEMP_C}°C[/]",
        f"[bold]Fan status[/bold]     [{fan_colour}]{fan_status}[/]",
    ]

    return Panel(
        "\n".join(lines),
        title="🌡  Environment",
        border_style="blue",
        expand=True,
    )


def _build_status_panel(active_zone, queue, photo_ts):
    """
    Builds the system status panel showing:
      - Currently active watering zone (if any)
      - Full queue of pending zones with estimated completion time
      - Morning cycle schedule info
      - Last photo timestamp
      - Current time (updates every refresh)
    """
    lines = []

    # Active watering zone
    if active_zone is not None:
        lines.append(f"[bold bright_cyan]💧 Watering:[/] {config.ZONE_LABELS[active_zone]}")
    else:
        lines.append("[dim]💧 Watering:  Idle[/]")

    # Queue with estimated completion time
    if queue:
        lines.append(f"[yellow]⏳ Queue ({len(queue)} zones):[/]")
        for i, z in enumerate(queue[:6]):   # Show max 6 to avoid overflow
            lines.append(f"   [dim]{i+1}.[/] {config.ZONE_LABELS[z]}")
        if len(queue) > 6:
            lines.append(f"   [dim]... +{len(queue)-6} more[/]")
        # Estimate: each zone takes WATER_DURATION_SECONDS + 2s gap
        est_secs = len(queue) * (config.WATER_DURATION_SECONDS + 2)
        est_mins = round(est_secs / 60, 1)
        lines.append(f"[dim]   Est. ~{est_mins} min to complete[/]")
    else:
        lines.append("[dim]⏳ Queue:     Empty[/]")

    lines.append("")

    # Morning cycle info
    lines.append(f"[bold]Morning cycle[/bold]  [green]07:00 daily — all 16 zones[/]")

    lines.append("")

    # Photo and time
    lines.append(f"[dim]📷 Last photo:  {photo_ts}[/]")
    lines.append(f"[dim]🕐 {datetime.datetime.now().strftime('%A  %H:%M:%S')}[/]")

    return Panel(
        "\n".join(lines),
        title="⚙  System Status",
        border_style="green",
        expand=True,
    )


def _build_error_panel():
    """
    Builds the recent errors panel showing the last 5 entries from error_log.
    Errors are colour-coded: red = ERROR level, yellow = WARNING level.
    If no errors exist, shows a green "all clear" message.
    """
    errors = db.get_recent_errors(limit=5)

    if not errors:
        return Panel(
            "[green]No recent errors ✓[/]",
            title="⚠  Recent Errors",
            border_style="dim",
            expand=True,
        )

    lines = []
    for e in errors:
        # Format: "04-09 14:01 [source] message (truncated to 80 chars)"
        ts     = e["timestamp"][5:16]   # Extract "MM-DD HH:MM" from ISO string
        src    = e["source"]  or "?"
        msg    = e["message"] or ""
        colour = "red" if e["level"] == "ERROR" else "yellow"
        lines.append(f"[{colour}]{ts} [{src}] {msg[:75]}[/]")

    return Panel(
        "\n".join(lines),
        title="⚠  Recent Errors",
        border_style="red",
        expand=True,
    )


# =============================================================================
# MAIN DASHBOARD LOOP
# =============================================================================

def _build_layout():
    """
    Assembles the complete screen layout from all panels.
    Called on every refresh cycle to get fresh data from the database.

    Layout structure:
      ┌─────────────────────────────────────────────────┐
      │         16-zone soil moisture table (70%)        │
      ├───────────────┬───────────────┬─────────────────┤
      │  Environment  │ System Status │  Recent Errors  │
      │    (30%)      │    (30%)      │     (40%)       │
      └───────────────┴───────────────┴─────────────────┘
    """
    # Fetch fresh data from SQLite for this render cycle
    readings     = db.get_latest_readings()
    active_zone  = irrigation.get_active_zone()
    queue        = irrigation.queue_snapshot()
    last_watered = {z: db.get_last_watered(z) for z in range(16)}
    _, photo_ts  = camera.get_latest_photo_info()

    # Build each panel
    zone_table = _build_zone_table(readings, last_watered, active_zone, queue)
    env_panel  = _build_environment_panel(readings)
    stat_panel = _build_status_panel(active_zone, queue, photo_ts)
    err_panel  = _build_error_panel()

    # Assemble into Rich Layout
    layout = Layout()
    layout.split_column(
        Layout(name="top",    ratio=7),   # 70% of screen height
        Layout(name="bottom", ratio=3),   # 30% of screen height
    )
    layout["top"].update(zone_table)
    layout["bottom"].split_row(
        Layout(env_panel,  name="env",    ratio=3),
        Layout(stat_panel, name="status", ratio=3),
        Layout(err_panel,  name="errors", ratio=4),
    )
    return layout


def run_dashboard():
    """
    Runs the Rich live dashboard on the main thread. Blocks indefinitely.

    Rich's Live context manager handles:
      - Clearing and redrawing the screen on each refresh
      - Thread-safe rendering (other threads can still log to console)
      - Restoring the terminal state cleanly on exit

    Waits DASHBOARD_START_DELAY seconds before starting so that sensor data
    exists in the database before the first render attempt.
    """
    time.sleep(config.DASHBOARD_START_DELAY)

    with Live(
        _build_layout(),
        console=console,
        refresh_per_second=1,   # Rich's internal refresh rate — 1Hz is enough
        screen=True,            # Full-screen mode — hides terminal cursor
    ) as live:
        while True:
            live.update(_build_layout())
            time.sleep(config.DASHBOARD_REFRESH_INTERVAL)

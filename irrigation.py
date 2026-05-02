# =============================================================================
# irrigation.py — SOLENOID QUEUE AND RELAY CONTROL
# =============================================================================
# Controls the 16 normally-closed solenoid valves via the MCP23017 GPIO
# expander and two 8-channel relay boards.
#
# HOW THE QUEUE WORKS:
#   - Zones are never watered in parallel — only ONE solenoid fires at a time.
#     This is intentional: the diaphragm pump maintains pressure better when
#     serving one solenoid, and it prevents pressure drops causing uneven flow.
#   - Any module can call request_water(zone, soil_raw) to queue a zone.
#   - A background thread (watering_worker) pulls zones off the queue one at
#     a time and fires each solenoid for WATER_DURATION_SECONDS.
#   - "Re-queue with reset timer" logic: if a zone is already waiting in the
#     queue when it gets requested again, its old entry is removed and a fresh
#     entry is added to the BACK of the queue with a new timestamp. This means
#     the zone moves to the back (fair to other zones) but gets a fresh timer.
#
# SOLENOID SAFETY:
#   - Solenoids are NORMALLY CLOSED — they fail safe (valve closed) with no power.
#   - The relay boards are ACTIVE LOW — GPIO LOW = relay energized = solenoid open.
#   - The finally block in watering_worker GUARANTEES the relay turns off even
#     if Python throws an exception mid-watering. A stuck-open solenoid would
#     flood the greenhouse, so this is the most critical safety feature.
#
# MCP23017 WIRING:
#   - Port A pins 0–7 → Relay Board 1 IN1–IN8 → Solenoids for Zones 0–7
#   - Port B pins 0–7 → Relay Board 2 IN1–IN8 → Solenoids for Zones 8–15
#   - MCP23017 communicates over I2C (SDA=BCM2, SCL=BCM3)
#
# RELAY BOARD WIRING (optocoupler isolated):
#   - JD-VCC jumper REMOVED (enables true isolation between Pi and 12V side)
#   - VCC     → 3.3V from Pi (powers optocoupler logic only)
#   - JD-VCC  → 5V PSU (powers relay coils independently)
#   - GND     → common ground bus bar
#   - IN1–8   → MCP23017 Port A or B pins
#   - COM     → 12V PSU positive
#   - NO      → Solenoid positive terminal
#   - Solenoid negative → 12V PSU negative → common ground bus bar
# =============================================================================

import time
import datetime
import threading
import board
import busio
from adafruit_mcp230xx.mcp23017 import MCP23017
from digitalio import Direction

import config
import db


# =============================================================================
# MODULE-LEVEL STATE
# =============================================================================

_relay_pins  = []        # List of 16 digitalio pin objects from MCP23017
_queue       = []        # List of (zone, soil_raw, queued_at) tuples
_queue_lock  = threading.Lock()   # Prevents race conditions between threads
_queued_zones = set()    # Set of zone numbers currently in the queue (fast lookup)
_active_zone  = None     # Zone number currently being watered, or None if idle


# =============================================================================
# SETUP
# =============================================================================

def setup_relays():
    """
    Initialises the MCP23017 over I2C and configures all 16 output pins.
    Sets all pins HIGH on startup (relay OFF = solenoid closed = no water).
    Must be called ONCE from main.py before any watering can occur.

    IMPORTANT — ENABLE I2C ON THE PI BEFORE RUNNING:
        sudo raspi-config → Interface Options → I2C → Enable
    """
    global _relay_pins

    # Initialise I2C bus using the Pi's hardware I2C pins
    i2c = busio.I2C(board.SCL, board.SDA)

    # Connect to MCP23017 at I2C address 0x20
    # Address 0x20 = A0, A1, A2 all tied to GND (default)
    mcp = MCP23017(i2c, address=config.MCP23017_I2C_ADDRESS)

    # Configure all 16 pins as digital outputs, starting in the OFF state
    # Pins 0–7  = Port A → Relay Board 1 → Zones 0–7
    # Pins 8–15 = Port B → Relay Board 2 → Zones 8–15
    _relay_pins = []
    for pin_num in range(16):
        pin = mcp.get_pin(pin_num)
        pin.direction = Direction.OUTPUT
        pin.value = True    # TRUE = HIGH = relay OFF (active-low relay board)
                            # All solenoids start CLOSED — safe default
        _relay_pins.append(pin)


def cleanup_relays():
    """
    Forces all 16 relay outputs HIGH (solenoids closed) before shutdown.
    Called from the SIGTERM/SIGINT handler in main.py to ensure no solenoid
    gets stuck open when the program exits or is killed by systemd.
    """
    for pin in _relay_pins:
        try:
            pin.value = True   # HIGH = relay OFF = solenoid closed
        except Exception:
            pass   # Best effort — don't raise during shutdown


# =============================================================================
# QUEUE MANAGEMENT
# =============================================================================

def request_water(zone, soil_raw):
    """
    Requests that a zone be added to the watering queue.

    RE-QUEUE WITH RESET TIMER LOGIC:
      - If zone is NOT in queue → append to back normally
      - If zone IS in queue already → remove old entry, append fresh entry
        to the BACK with a new timestamp (moves it to end, resets wait timer)
      - If zone is currently being actively watered → ignore (it's already running)

    soil_raw: the moisture reading that triggered this request, or None for
              the morning cycle (no threshold trigger).

    This function is thread-safe — uses a lock to prevent the watering_worker
    thread from modifying the queue simultaneously.
    """
    global _queued_zones

    with _queue_lock:
        # Don't queue a zone that's currently firing — it's already open
        if zone == _active_zone:
            return

        # Remove existing queue entry if this zone is already waiting
        # This implements the "re-queue, reset timer" behaviour
        if zone in _queued_zones:
            for i, entry in enumerate(_queue):
                if entry[0] == zone:
                    _queue.pop(i)
                    break
            # _queued_zones entry will be re-added below

        # Add (or re-add) zone to the back of the queue with current timestamp
        _queue.append((zone, soil_raw, datetime.datetime.now()))
        _queued_zones.add(zone)


def _pop_next():
    """
    Removes and returns the next zone from the front of the queue.
    Returns None if the queue is empty.
    Thread-safe — uses _queue_lock.
    """
    with _queue_lock:
        if not _queue:
            return None
        entry = _queue.pop(0)
        _queued_zones.discard(entry[0])
        return entry   # Returns (zone, soil_raw, queued_at)


def queue_snapshot():
    """
    Returns a copy of the current queue as a list of zone numbers, in order.
    Used by dashboard.py to display the queue status panel.
    Thread-safe read — uses _queue_lock.
    """
    with _queue_lock:
        return [entry[0] for entry in _queue]


def get_active_zone():
    """
    Returns the zone number currently being watered, or None if idle.
    Used by dashboard.py to show the "currently watering" status.
    """
    return _active_zone


# =============================================================================
# WATERING WORKER — runs in its own daemon thread
# =============================================================================

def watering_worker():
    """
    The main irrigation loop — runs forever in a background daemon thread.
    Pulls one zone at a time from the queue and fires its solenoid.

    THREAD SAFETY:
      This function runs in its own thread. It only modifies _active_zone
      and uses _pop_next() (which is locked) to access the queue.
      The rest of the system reads _active_zone and queue_snapshot() for
      display purposes — these reads are safe without locks because setting
      a Python reference is atomic.

    SOLENOID FIRE SEQUENCE PER ZONE:
      1. Pop zone from queue front
      2. Record when it came off the queue (to calculate queue_wait_seconds)
      3. Set _active_zone so dashboard shows it as currently watering
      4. Pull relay pin LOW → relay energizes → solenoid opens → water flows
      5. Sleep for WATER_DURATION_SECONDS
      6. ALWAYS (even on exception) pull relay pin HIGH → solenoid closes
      7. Log the watering event to the database
      8. Sleep 2 seconds to let pipe pressure stabilise before next zone

    THE FINALLY BLOCK IS CRITICAL:
      The relay turns off inside a finally block, which runs even if Python
      raises an exception during the sleep. This means a solenoid can NEVER
      get stuck open due to a software error. Without this, a crash mid-water
      would leave a valve open indefinitely and flood the greenhouse.
    """
    global _active_zone

    while True:
        entry = _pop_next()

        if entry is None:
            # Queue is empty — sleep briefly before checking again
            # 1 second granularity is fine for irrigation
            time.sleep(1)
            continue

        zone, soil_raw, queued_at = entry
        queue_wait = (datetime.datetime.now() - queued_at).total_seconds()
        pin = _relay_pins[zone]

        _active_zone = zone

        try:
            # Pull LOW → relay ON → solenoid energized → valve opens → water flows
            pin.value = False
            time.sleep(config.WATER_DURATION_SECONDS)

        except Exception as e:
            db.log_error("irrigation", f"Zone {zone} solenoid error during watering: {e}")

        finally:
            # THIS ALWAYS RUNS — even if an exception was raised above.
            # Pulls HIGH → relay OFF → solenoid de-energized → valve closes.
            # This is the single most important safety line in the entire codebase.
            pin.value = True
            _active_zone = None

            # Log the completed watering event to the database
            db.log_watering(zone, config.WATER_DURATION_SECONDS, soil_raw, queue_wait)

            # Wait 2 seconds before next zone to let the pump pressure stabilise
            # and prevent water hammer in the PVC when rapidly switching valves
            time.sleep(2)

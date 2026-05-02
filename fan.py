# =============================================================================
# fan.py — FAN RELAY CONTROL
# =============================================================================
# Controls a single relay-driven fan based on the inside greenhouse temperature
# read from the DHT22 sensor.
#
# The fan turns ON when temperature exceeds FAN_ON_TEMP_C (default 30°C).
# The fan turns OFF when temperature drops below FAN_OFF_TEMP_C (default 27°C).
#
# WHY TWO DIFFERENT THRESHOLDS (HYSTERESIS)?
#   Without a gap between the ON and OFF thresholds, if the temperature sits
#   right at 30°C it would flicker: 30.1°→ON, 29.9°→OFF, 30.0°→ON, 29.8°→OFF.
#   This rapid switching stresses both the relay contacts and the fan motor.
#   With hysteresis, once the fan turns on at 30°C it stays on until the
#   temperature drops all the way to 27°C — much gentler on the hardware.
#
# FAN WIRING (direct GPIO, not through MCP23017):
#   Relay IN  → BCM22 (Physical Pin 15) — defined as FAN_RELAY_PIN in config
#   Relay VCC → 3.3V from Pi
#   Relay GND → common ground bus bar
#   Relay COM → 12V PSU positive
#   Relay NO  → Fan positive terminal
#   Fan negative → 12V PSU negative → common ground bus bar
#
# RELAY LOGIC:
#   Active-LOW relay board: GPIO LOW = relay ON = fan runs
#                           GPIO HIGH = relay OFF = fan stops
# =============================================================================

import RPi.GPIO as GPIO
import config
import db

# Tracks current fan state so we don't repeatedly toggle the relay
_fan_on = False


def setup_fan():
    """
    Configures the fan relay GPIO pin as an output and ensures the fan
    starts in the OFF state. Called once from main.py at startup.

    Uses RPi.GPIO (not CircuitPython) because the fan is a single direct
    GPIO pin, not going through the MCP23017 I2C expander.
    GPIO.BCM mode is set in main.py — do not call GPIO.setmode() here.
    """
    GPIO.setup(config.FAN_RELAY_PIN, GPIO.OUT)
    GPIO.output(config.FAN_RELAY_PIN, GPIO.HIGH)   # HIGH = relay OFF = fan stopped


def update_fan(temp_inside):
    """
    Checks the current inside temperature against thresholds and toggles
    the fan relay if needed. Called from the sensor loop in main.py every
    time a DHT22 reading completes.

    temp_inside: float (Celsius) or None if the DHT22 read failed.

    If temp_inside is None (bad sensor read), the fan state is NOT changed.
    It's safer to leave the fan in its current state than to make a decision
    based on missing data.

    Fan state changes are logged to the error_log table with level WARNING
    so they appear on the dashboard and in Excel exports. This lets you
    see patterns like "fan runs every afternoon between 2–5pm".
    """
    global _fan_on

    if temp_inside is None:
        return   # No data — don't change fan state

    try:
        # Check if fan should turn ON (currently off, temp at or above threshold)
        if not _fan_on and temp_inside >= config.FAN_ON_TEMP_C:
            GPIO.output(config.FAN_RELAY_PIN, GPIO.LOW)   # LOW = relay ON = fan runs
            _fan_on = True
            db.log_error(
                "fan",
                f"Fan turned ON — inside temp {temp_inside}°C >= {config.FAN_ON_TEMP_C}°C threshold",
                level="WARNING"   # WARNING not ERROR — this is normal operation
            )

        # Check if fan should turn OFF (currently on, temp dropped below lower threshold)
        elif _fan_on and temp_inside < config.FAN_OFF_TEMP_C:
            GPIO.output(config.FAN_RELAY_PIN, GPIO.HIGH)  # HIGH = relay OFF = fan stops
            _fan_on = False
            db.log_error(
                "fan",
                f"Fan turned OFF — inside temp {temp_inside}°C < {config.FAN_OFF_TEMP_C}°C threshold",
                level="WARNING"   # WARNING not ERROR — this is normal operation
            )
        # If neither condition is true, fan state stays unchanged (hysteresis zone)

    except Exception as e:
        db.log_error("fan", f"Fan relay GPIO error: {e}")


def is_fan_on():
    """
    Returns True if the fan is currently running, False if stopped.
    Used by dashboard.py to display the fan status in the environment panel.
    """
    return _fan_on


def cleanup_fan():
    """
    Ensures the fan relay is turned OFF before program exit.
    Called from the shutdown handler in main.py alongside cleanup_relays().
    """
    try:
        GPIO.output(config.FAN_RELAY_PIN, GPIO.HIGH)   # HIGH = relay OFF = fan stopped
    except Exception:
        pass   # Best effort during shutdown — don't raise

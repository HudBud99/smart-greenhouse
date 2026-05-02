# =============================================================================
# sensors.py — SENSOR READING MODULE
# =============================================================================
# Handles all hardware sensor communication:
#   - 16 capacitive soil moisture sensors via 2x MCP3008 ADC chips (SPI)
#   - 2x DHT22 temperature and humidity sensors (single-wire protocol)
#
# WHY MCP3008?
#   The Raspberry Pi has no analog input pins — it can only read digital
#   (HIGH or LOW). Soil sensors output an analog voltage that varies with
#   moisture level. The MCP3008 converts that analog voltage to a 10-bit
#   digital number (0–1023) that the Pi can read over SPI.
#   Two MCP3008 chips gives us 16 analog channels total (8 per chip).
#
# SPI WIRING (same bus, different CS pins):
#   MOSI  → BCM10 (Physical 19) — Pi sends data to MCP3008
#   MISO  → BCM9  (Physical 21) — MCP3008 sends data to Pi
#   SCLK  → BCM11 (Physical 23) — clock signal synchronises transfer
#   CE0   → BCM8  (Physical 24) — selects MCP3008 #1 (zones 0–7)
#   CE1   → BCM7  (Physical 26) — selects MCP3008 #2 (zones 8–15)
#   VCC   → 3.3V
#   GND   → common ground bus bar
#
# DHT22 WIRING:
#   Inside  DATA → BCM17 (Physical 11)
#   Outside DATA → BCM27 (Physical 13)
#   VCC → 3.3V    GND → common ground bus bar
#   Add a 10kΩ pull-up resistor between DATA and 3.3V on each sensor
#
# IMPORTANT — ENABLE SPI ON THE PI BEFORE RUNNING:
#   sudo raspi-config → Interface Options → SPI → Enable
# =============================================================================

import time
import board
import busio
import digitalio
import adafruit_dht
import adafruit_mcp3xxx.mcp3008 as MCP
from adafruit_mcp3xxx.analog_in import AnalogIn

import config
import db


# =============================================================================
# MCP3008 SETUP
# =============================================================================
# These are module-level variables — set up once in setup_sensors() and then
# used by read_all_soil() on every subsequent call.

_spi      = None   # The shared SPI bus object
_mcps     = []     # List of two MCP3008 chip objects [chip0, chip1]
_channels = []     # Flat list of 16 AnalogIn channel objects, indexed by zone


def setup_sensors():
    """
    Initialises the SPI bus and both MCP3008 ADC chips.
    Must be called ONCE from main.py at startup before any soil reads.

    Creates a flat list of 16 AnalogIn channel objects:
      _channels[0]  = MCP3008 #1, channel 0 → soil sensor for zone 0
      _channels[1]  = MCP3008 #1, channel 1 → soil sensor for zone 1
      ...
      _channels[7]  = MCP3008 #1, channel 7 → soil sensor for zone 7
      _channels[8]  = MCP3008 #2, channel 0 → soil sensor for zone 8
      ...
      _channels[15] = MCP3008 #2, channel 7 → soil sensor for zone 15
    """
    global _spi, _mcps, _channels

    # Initialise the hardware SPI bus using CircuitPython board pin names
    # clock=SCK, MISO=MISO, MOSI=MOSI are the hardware SPI pins on the Pi
    _spi = busio.SPI(clock=board.SCK, MISO=board.MISO, MOSI=board.MOSI)

    # Create one MCP3008 object per chip, each with its own CS (chip select) pin
    for cs_bcm in config.MCP3008_CS_PINS:
        # Convert BCM pin number to CircuitPython DigitalInOut object
        cs  = digitalio.DigitalInOut(getattr(board, f"D{cs_bcm}"))
        mcp = MCP.MCP3008(_spi, cs)
        _mcps.append(mcp)

    # Build the flat 16-channel list by iterating both chips × 8 channels
    _channels = []
    for mcp in _mcps:
        for ch in range(8):
            # MCP.P0 through MCP.P7 are the channel constants in the library
            attr = getattr(MCP, f"P{ch}")
            _channels.append(AnalogIn(mcp, attr))


def _read_channel(zone, retries=3):
    """
    Reads a single soil sensor channel with automatic retry logic.

    The MCP3008 occasionally returns a bad value due to SPI noise in
    a humid greenhouse environment. We retry up to 3 times with a short
    delay before giving up and returning None.

    Returns:
        int: raw ADC value 0–1023 on success
        None: if all retry attempts fail (caller must handle safely)

    IMPORTANT: Callers must treat None as "unknown — do NOT water this zone".
    Watering on a bad read could mean watering soil that's already saturated.
    """
    for attempt in range(retries):
        try:
            # .value returns 0–65535 (16-bit). Right-shift 6 bits to get
            # 10-bit range 0–1023 matching the MCP3008's actual resolution.
            raw = _channels[zone].value >> 6

            # Sanity check — should always be in range, but validate anyway
            if 0 <= raw <= 1023:
                return raw

        except Exception as e:
            db.log_error(
                "sensors",
                f"Zone {zone} read attempt {attempt + 1}/{retries} failed: {e}",
                level="WARNING"
            )
            time.sleep(0.1)   # Brief pause before retry

    # All retries exhausted — log a full error and return None
    db.log_error("sensors", f"Zone {zone} failed all {retries} read attempts — skipping")
    return None


def read_all_soil():
    """
    Reads all 16 soil moisture sensors in order, zones 0 through 15.
    Returns a list of 16 values: each is either an int (0–1023) or None.

    A None value means that zone's sensor could not be read after all retries.
    The irrigation and logging code treats None as "skip this zone" to avoid
    watering decisions based on bad data.

    A 20ms pause between individual reads reduces SPI bus noise from
    multiple rapid reads in a humid environment.
    """
    readings = []
    for zone in range(16):
        val = _read_channel(zone)
        readings.append(val)
        time.sleep(0.02)   # 20ms gap between reads to settle the SPI bus
    return readings


# =============================================================================
# DHT22 SETUP
# =============================================================================

_dht_inside  = None   # DHT22 sensor object for inside the greenhouse
_dht_outside = None   # DHT22 sensor object for outside the greenhouse


def setup_dht():
    """
    Initialises both DHT22 temperature and humidity sensors.
    Must be called ONCE from main.py at startup before any DHT22 reads.

    Uses CircuitPython's adafruit_dht library which handles the strict
    single-wire timing protocol that DHT22 sensors require.
    """
    global _dht_inside, _dht_outside
    _dht_inside  = adafruit_dht.DHT22(getattr(board, f"D{config.DHT_INSIDE_PIN}"))
    _dht_outside = adafruit_dht.DHT22(getattr(board, f"D{config.DHT_OUTSIDE_PIN}"))


def _read_dht(sensor, label, retries=3):
    """
    Reads one DHT22 sensor with retry logic.

    DHT22 QUIRKS TO BE AWARE OF:
      1. Minimum 2-second gap between reads — enforced by time.sleep(2) here.
         Polling faster than 0.5Hz causes the sensor to return errors.
      2. RuntimeError is NORMAL for DHT22 — the sensor occasionally sends
         a bad checksum. This is expected behaviour, not a hardware fault.
         We catch it silently and retry rather than logging every occurrence.
      3. The sensor sometimes returns None values even without raising an
         exception — we check for this explicitly before returning.

    Returns:
        tuple: (temperature_celsius: float, humidity_percent: float) on success
        tuple: (None, None) if all retries fail
    """
    for attempt in range(retries):
        try:
            temp = sensor.temperature   # Returns float in Celsius
            hum  = sensor.humidity      # Returns float 0–100

            # Validate that we got actual numbers back, not None
            if temp is not None and hum is not None:
                return round(temp, 1), round(hum, 1)

        except RuntimeError:
            # RuntimeError is the DHT22's normal "bad read" signal.
            # Don't log this as an error — just retry after the mandatory delay.
            pass

        except Exception as e:
            # Unexpected error — log it and stop retrying this cycle
            db.log_error("sensors", f"DHT22 {label} unexpected error: {e}")
            break

        # DHT22 REQUIRES at least 2 seconds between reads — non-negotiable
        time.sleep(2)

    # All retries failed — log a warning and return None tuple
    db.log_error(
        "sensors",
        f"DHT22 {label} (pin BCM{config.DHT_INSIDE_PIN if label == 'inside' else config.DHT_OUTSIDE_PIN}) "
        f"failed all {retries} read attempts",
        level="WARNING"
    )
    return None, None


def read_dht_inside():
    """
    Reads the inside greenhouse DHT22 sensor.
    Returns (temperature_c, humidity_pct) or (None, None) on failure.
    """
    return _read_dht(_dht_inside, "inside")


def read_dht_outside():
    """
    Reads the outside DHT22 sensor.
    Returns (temperature_c, humidity_pct) or (None, None) on failure.
    """
    return _read_dht(_dht_outside, "outside")

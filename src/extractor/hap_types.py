"""HomeKit type, category and value tables for the ZMKF reader.

Names follow the HomeKit Accessory Protocol specification. Only the codes this file is sure of are named; any other
Apple-base type stays `hap:<code>`, and any other UUID is `vendor:<uuid>`.
"""

from __future__ import annotations
import struct
from typing import Any, Optional

APPLE_BASE = "-0000-1000-8000-0026BB765291"

SERVICE_NAMES = {
    0x3E: "AccessoryInformation", 0x43: "Lightbulb", 0x47: "Outlet", 0x49: "Switch", 0x55: "Pairing",
    0x80: "ContactSensor", 0x82: "HumiditySensor", 0x84: "LightSensor", 0x85: "MotionSensor",
    0x86: "OccupancySensor", 0x8A: "TemperatureSensor", 0x96: "Battery", 0xA2: "ProtocolInformation",
    0xD8: "Television", 0xD9: "InputSource", 0xDA: "AccessControl", 0x110: "CameraRTPStreamManagement",
    0x112: "Microphone", 0x113: "Speaker", 0x129: "DataStreamTransportManagement",
    0x204: "CameraRecordingManagement", 0x21A: "CameraOperatingMode", 0x221: "PowerManagement",
    0x22A: "WiFiTransport", 0x236: "FirmwareUpdate", 0x239: "AccessoryRuntimeInformation",
    0x701: "ThreadTransport",
}

CHARACTERISTIC_NAMES = {
    0x08: "Brightness", 0x10: "CurrentRelativeHumidity", 0x11: "CurrentTemperature", 0x13: "Hue", 0x14: "Identify",
    0x20: "Manufacturer", 0x21: "Model", 0x22: "MotionDetected", 0x23: "Name", 0x25: "On", 0x2F: "Saturation",
    0x30: "SerialNumber", 0x37: "Version", 0x52: "FirmwareRevision", 0x53: "HardwareRevision", 0x68: "BatteryLevel",
    0x6A: "ContactSensorState", 0x6B: "CurrentAmbientLightLevel", 0x71: "OccupancyDetected", 0x75: "StatusActive",
    0x79: "StatusLowBattery", 0x8F: "ChargingState", 0xA6: "AccessoryFlags", 0xB0: "Active",
    0xCE: "ColorTemperature", 0xD6: "IsConfigured", 0xDB: "InputSourceType", 0xE1: "RemoteKey",
    0xE3: "ConfiguredName", 0xE8: "SleepDiscoveryMode", 0x119: "Volume", 0x11A: "Mute", 0x11B: "NightVision",
    0x220: "ProductData", 0x22B: "CurrentTransport", 0x702: "ThreadNodeCapabilities", 0x703: "ThreadStatus",
    0x704: "ThreadControlPoint", 0x706: "ThreadOpenThreadVersion",
}

# HAP accessory categories; a number not listed stays a number
CATEGORY_NAMES = {
    1: "Other", 2: "Bridge", 3: "Fan", 4: "GarageDoorOpener", 5: "Lightbulb", 6: "DoorLock", 7: "Outlet",
    8: "Switch", 9: "Thermostat", 10: "Sensor", 11: "SecuritySystem", 12: "Door", 13: "Window",
    14: "WindowCovering", 15: "ProgrammableSwitch", 16: "RangeExtender", 17: "IPCamera", 18: "VideoDoorbell",
    19: "AirPurifier", 20: "Heater", 21: "AirConditioner", 22: "Humidifier", 23: "Dehumidifier", 24: "AppleTV",
    25: "HomePod", 26: "Speaker", 27: "AirPort", 28: "Sprinkler", 29: "Faucet", 30: "ShowerHead", 31: "Television",
    32: "TargetController",
}

# Inferred from observed stores, not documented by Apple
PROTOCOLS = {1: "HomeKit (HAP)", 2: "Matter"}
LINK_LAYERS = {16: "Thread", 2: "Wi-Fi"}

SENSES = {"OccupancySensor": "Occupancy", "MotionSensor": "Motion", "LightSensor": "LightLevel",
          "ContactSensor": "Contact", "TemperatureSensor": "Temperature", "HumiditySensor": "Humidity"}


def hap_code(type_uuid: str) -> Optional[int]:
    """The short code of an Apple-base type UUID, else None."""
    text = type_uuid.strip().upper()
    if len(text) == 32 and "-" not in text:  # undashed form
        text = f"{text[:8]}-{text[8:12]}-{text[12:16]}-{text[16:20]}-{text[20:]}"
    if not text.endswith(APPLE_BASE) or len(text) != 36:
        return None
    try:
        return int(text[:8], 16)
    except ValueError:
        return None


def type_name(type_uuid: str, table: dict) -> str:
    code = hap_code(type_uuid)
    if code is None:
        return f"vendor:{type_uuid}" if type_uuid else ""
    return table.get(code, f"hap:{code:08X}")


def decode_opack(raw: Any) -> Any:
    """The OPACK subset HomeKit stores values in; anything else stays "hex:<bytes>"."""
    if not isinstance(raw, (bytes, bytearray)):
        return raw
    data = bytes(raw)
    if not data:
        return "hex:"
    tag, body = data[0], data[1:]
    sizes = {0x30: "<b", 0x31: "<h", 0x32: "<i", 0x33: "<q", 0x35: "<f", 0x36: "<d"}
    if len(data) == 1 and tag in (0x01, 0x02):
        return tag == 0x01
    if len(data) == 1 and 0x08 <= tag <= 0x2F:
        return tag - 0x08
    if tag in sizes and len(body) == struct.calcsize(sizes[tag]):
        value = struct.unpack(sizes[tag], body)[0]
        return float(value) if tag in (0x35, 0x36) else value
    return f"hex:{data.hex()}"

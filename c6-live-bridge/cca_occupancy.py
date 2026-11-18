"""Occupancy state from received LRF2 sensor frames (CCA format 0x0E).

Byte 12 is the state (0x03 occupied, 0x00 vacant); byte 11 is 0x06 for a state
change and 0x26 for a status report carrying the current state. Confirmed
against RA3 occupancy groups on 2026-11-17 (docs/protocols/cca/index.md in the
protocol repository). Anything else is left undecoded rather than guessed.
"""


def decode(raw_hex):
    """Return {"serial", "occupied", "report"} for a format 0x0E sensor frame, else None."""
    try: b = bytes.fromhex(raw_hex)
    except (TypeError, ValueError): return None
    if len(b) < 20 or not 0x88 <= b[0] <= 0x8b or b[7] != 0x0e or b[8] != 0x04 or b[10] != 0x02: return None
    if b[2:6] != b[13:17] or b[18] != 0x47 or b[11] not in (0x06, 0x26) or b[12] not in (0x00, 0x03): return None
    serial = int.from_bytes(b[2:6], "big")
    if not serial: return None
    return {"serial": serial, "occupied": b[12] == 0x03, "report": "change" if b[11] == 0x06 else "status"}


# Byte 19 for each (report, occupied) pair, as observed on every captured frame.
_PARAM = {("change", True): 0x00, ("change", False): 0x01, ("status", True): 0x06, ("status", False): 0x07}


def _serial(serial):
    if isinstance(serial, bool) or not isinstance(serial, int) or not 0 < serial <= 0xffffffff: raise ValueError("serial")
    return serial.to_bytes(4, "big")


def encode(serial, occupied, report="change", event=0, tail=b"\xff\x00"):
    """Hex of a format 0x0E frame without CRC; the radio node adds the CRC and sets byte 1.

    event selects the type byte (0x88 + event % 4). Real sensors advance it by one for
    each new state change and reuse the current value for status reports. tail is
    bytes 20-21, whose meaning is not decoded.
    """
    if report not in ("change", "status") or not isinstance(occupied, bool): raise ValueError("state")
    if isinstance(event, bool) or not isinstance(event, int) or event < 0: raise ValueError("event")
    tail = bytes(tail)
    if len(tail) != 2: raise ValueError("tail")
    s = _serial(serial)
    return (bytes((0x88 + event % 4, 0x00)) + s + bytes((0x21, 0x0e, 0x04, 0x00, 0x02,
        0x06 if report == "change" else 0x26, 0x03 if occupied else 0x00)) + s
        + bytes((0x00, 0x47, _PARAM[(report, occupied)])) + tail).hex()


def encode_announce(serial):
    """Hex of the 53-byte activation announce (type 0xBA, format 0x21) without CRC.

    Layout from one LRF2 ceiling sensor captured during RA3 activation on 2026-11-17.
    """
    s = _serial(serial)
    return (bytes((0xba, 0x00)) + s + bytes((0x21, 0x21, 0x00)) + b"\xff" * 5 + bytes((0x0d, 0x05)) + s + s
        + bytes(4) + bytes((0x07, 0x00, 0x00, 0x06, 0x08, 0x01, 0x01, 0x00, 0x00)) + b"\xff" * 4 + b"\xcc" * 10).hex()

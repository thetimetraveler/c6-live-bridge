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

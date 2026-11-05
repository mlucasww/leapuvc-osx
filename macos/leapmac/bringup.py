"""Replay a recorded control-write sequence to wake the Leap's video stream.

OpenMotion reports that f182:0003 boots inert: it enumerates and negotiates,
but sends nothing until these writes arrive. They all travel on endpoint 0, so
no root access or interface claim should be needed.
"""

from dataclasses import dataclass
from pathlib import Path

from .uvc_control import LibusbError

SEQUENCE_PATH = Path(__file__).resolve().parents[1] / "third_party" / "openmotion" / "bringup_seq.txt"

# In the recording, leapd's device reset falls at this row, which is a stray
# SET_INTERFACE. OpenMotion resets here and skips that row.
RESET_SPLIT = 52


@dataclass(frozen=True)
class ControlWrite:
    request_type: int
    request: int
    value: int
    index: int
    data: bytes

    def describe(self):
        return (
            f"{self.request_type:#04x} {self.request:#04x} {self.value:#06x} "
            f"{self.index:#06x} {self.data.hex() or '-'}"
        )


def parse_sequence(text):
    """Parse sequence text. A malformed row raises, so it can never be sent as zero."""
    rows = []
    for number, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) != 5:
            raise ValueError(f"line {number}: expected 5 fields, got {len(fields)}")
        request_type, request, value, index = (int(field, 16) for field in fields[:4])
        data = b"" if fields[4] == "-" else bytes.fromhex(fields[4])
        rows.append(ControlWrite(request_type, request, value, index, data))
    return rows


def load_sequence(path=SEQUENCE_PATH):
    return parse_sequence(Path(path).read_text())


def replay(device, rows):
    """Send each row in order, and carry on past failures while recording them."""
    failures = []
    for position, row in enumerate(rows):
        try:
            device.write_raw(row.request_type, row.request, row.value, row.index, row.data)
        except LibusbError as error:
            failures.append({"position": position, "row": row.describe(), "error": str(error)})
    return {"attempted": len(rows), "failed": len(failures), "failures": failures}

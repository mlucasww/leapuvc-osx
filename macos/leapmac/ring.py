"""Read frames from the shared-memory ring that leapcap writes.

The layout is documented on FrameRing in leapcap/leapcap.swift. This reader
checks seq_end before copying a slot and seq_begin after, so a slot the writer
was overwriting during the copy is rejected and never used.
"""

import mmap
import struct
from dataclasses import dataclass
from pathlib import Path

MAGIC = 0x5041454C
VERSION = 1
HEADER_SIZE = 64
SLOT_HEADER_SIZE = 32

_HEADER = struct.Struct("<IIIIII")
_U32 = struct.Struct("<I")
_U64 = struct.Struct("<Q")

LATEST_OFFSET = 24
STRIDE_OFFSET = 32
PIXEL_FORMAT_OFFSET = 36

SLOT_BEGIN_OFFSET = 0
SLOT_END_OFFSET = 8
SLOT_PTS_OFFSET = 16
SLOT_LENGTH_OFFSET = 24

FOURCC_NAMES = {0x79757673: "yuvs", 0x32767579: "2vuy"}


@dataclass(frozen=True)
class RingFrame:
    sequence: int
    pts_ns: int
    payload: bytes


class FrameRing:
    def __init__(self, path):
        self.path = Path(path)
        self._file = open(self.path, "rb")
        try:
            self._map = mmap.mmap(self._file.fileno(), 0, access=mmap.ACCESS_READ)
        except Exception:
            self._file.close()
            raise

        magic, version, self.width, self.height, self.slot_bytes, self.slot_count = _HEADER.unpack_from(self._map, 0)
        if magic != MAGIC:
            self.close()
            raise ValueError(f"{self.path} is not a frame ring")
        if version != VERSION:
            self.close()
            raise ValueError(f"unsupported ring version {version}")

        self.slot_stride = _U32.unpack_from(self._map, STRIDE_OFFSET)[0]
        fourcc = _U32.unpack_from(self._map, PIXEL_FORMAT_OFFSET)[0]
        self.pixel_format = FOURCC_NAMES.get(fourcc, f"{fourcc:#010x}")
        self.torn = 0

    def close(self):
        self._map.close()
        self._file.close()

    def latest_sequence(self):
        return _U64.unpack_from(self._map, LATEST_OFFSET)[0]

    def read_latest(self, after=0):
        """Return the newest frame if it is newer than `after`, otherwise None.

        This suits a slow consumer that only wants fresh data.
        """
        sequence = self.latest_sequence()
        if sequence <= after:
            return None
        return self._read_slot(sequence)

    def read_next(self, after=0):
        """Return the frame after `after`, so a brief stall loses nothing.

        With `after` of 0 this starts at the newest frame. A reader that falls
        too far behind jumps forward to the oldest slot that's still safe, so
        the caller sees the gap in the sequence numbers.
        """
        latest = self.latest_sequence()
        if latest <= after:
            return None
        if after == 0:
            return self._read_slot(latest)
        # The writer's next frame replaces latest + 1 - slot_count, so stay clear of it.
        oldest_safe = max(1, latest - self.slot_count + 2)
        return self._read_slot(max(after + 1, oldest_safe))

    def _read_slot(self, sequence):
        """Copy one slot. A frame overwritten during the copy counts as torn."""
        slot = HEADER_SIZE + (sequence % self.slot_count) * self.slot_stride
        if _U64.unpack_from(self._map, slot + SLOT_END_OFFSET)[0] != sequence:
            self.torn += 1
            return None

        pts_ns = _U64.unpack_from(self._map, slot + SLOT_PTS_OFFSET)[0]
        length = _U32.unpack_from(self._map, slot + SLOT_LENGTH_OFFSET)[0]
        if length > self.slot_bytes:
            self.torn += 1
            return None

        start = slot + SLOT_HEADER_SIZE
        payload = self._map[start : start + length]

        if _U64.unpack_from(self._map, slot + SLOT_BEGIN_OFFSET)[0] != sequence:
            self.torn += 1
            return None
        return RingFrame(sequence, pts_ns, payload)

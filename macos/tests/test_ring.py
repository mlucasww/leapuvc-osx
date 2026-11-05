import struct

import pytest

from leapmac import ring

WIDTH, HEIGHT, SLOTS = 4, 2, 4
PAYLOAD = WIDTH * HEIGHT * 2
STRIDE = (ring.SLOT_HEADER_SIZE + PAYLOAD + 63) & ~63
YUVS = 0x79757673


class FakeWriter:
    """Writes the same layout as FrameRing in leapcap.swift."""

    def __init__(self, path, magic=ring.MAGIC, version=ring.VERSION):
        self.path = path
        self.data = bytearray(ring.HEADER_SIZE + SLOTS * STRIDE)
        struct.pack_into("<IIIIII", self.data, 0, magic, version, WIDTH, HEIGHT, PAYLOAD, SLOTS)
        struct.pack_into("<I", self.data, ring.STRIDE_OFFSET, STRIDE)
        struct.pack_into("<I", self.data, ring.PIXEL_FORMAT_OFFSET, YUVS)
        self.sequence = 0
        self.flush()

    def flush(self):
        self.path.write_bytes(bytes(self.data))

    def slot(self, sequence):
        return ring.HEADER_SIZE + (sequence % SLOTS) * STRIDE

    def write(self, value):
        self.sequence += 1
        slot = self.slot(self.sequence)
        struct.pack_into("<QQQI", self.data, slot, self.sequence, self.sequence, self.sequence * 1000, PAYLOAD)
        self.data[slot + ring.SLOT_HEADER_SIZE : slot + ring.SLOT_HEADER_SIZE + PAYLOAD] = bytes([value]) * PAYLOAD
        struct.pack_into("<Q", self.data, ring.LATEST_OFFSET, self.sequence)
        self.flush()


def test_header_is_read(tmp_path):
    path = tmp_path / "frames.ring"
    FakeWriter(path)
    frames = ring.FrameRing(path)
    assert (frames.width, frames.height, frames.slot_count) == (WIDTH, HEIGHT, SLOTS)
    assert frames.slot_stride == STRIDE
    assert frames.pixel_format == "yuvs"
    frames.close()


def test_an_empty_ring_has_no_frame(tmp_path):
    path = tmp_path / "frames.ring"
    FakeWriter(path)
    frames = ring.FrameRing(path)
    assert frames.read_latest() is None
    assert frames.torn == 0
    frames.close()


def test_latest_frame_is_returned_once(tmp_path):
    path = tmp_path / "frames.ring"
    writer = FakeWriter(path)
    writer.write(7)
    frames = ring.FrameRing(path)

    frame = frames.read_latest()

    assert frame.sequence == 1
    assert frame.pts_ns == 1000
    assert frame.payload == bytes([7]) * PAYLOAD
    assert frames.read_latest(after=frame.sequence) is None
    frames.close()


def test_reader_jumps_to_the_newest_frame_after_wrapping(tmp_path):
    path = tmp_path / "frames.ring"
    writer = FakeWriter(path)
    for value in range(SLOTS + 3):
        writer.write(value)
    frames = ring.FrameRing(path)

    frame = frames.read_latest(after=2)

    assert frame.sequence == SLOTS + 3
    assert frame.payload == bytes([SLOTS + 2]) * PAYLOAD
    frames.close()


def test_a_slot_being_overwritten_is_rejected(tmp_path):
    path = tmp_path / "frames.ring"
    writer = FakeWriter(path)
    writer.write(1)
    # The writer has started on this slot: seq_begin moved, seq_end hasn't.
    struct.pack_into("<Q", writer.data, writer.slot(1) + ring.SLOT_BEGIN_OFFSET, 1 + SLOTS)
    writer.flush()
    frames = ring.FrameRing(path)

    assert frames.read_latest() is None
    assert frames.torn == 1
    frames.close()


def test_a_slot_not_yet_finished_is_rejected(tmp_path):
    path = tmp_path / "frames.ring"
    writer = FakeWriter(path)
    writer.write(1)
    struct.pack_into("<Q", writer.data, writer.slot(1) + ring.SLOT_END_OFFSET, 0)
    writer.flush()
    frames = ring.FrameRing(path)

    assert frames.read_latest() is None
    assert frames.torn == 1
    frames.close()


def test_a_bad_length_is_rejected(tmp_path):
    path = tmp_path / "frames.ring"
    writer = FakeWriter(path)
    writer.write(1)
    struct.pack_into("<I", writer.data, writer.slot(1) + ring.SLOT_LENGTH_OFFSET, PAYLOAD + 1)
    writer.flush()
    frames = ring.FrameRing(path)

    assert frames.read_latest() is None
    assert frames.torn == 1
    frames.close()


def test_wrong_magic_is_rejected(tmp_path):
    path = tmp_path / "frames.ring"
    FakeWriter(path, magic=0)
    with pytest.raises(ValueError):
        ring.FrameRing(path)


def test_wrong_version_is_rejected(tmp_path):
    path = tmp_path / "frames.ring"
    FakeWriter(path, version=99)
    with pytest.raises(ValueError):
        ring.FrameRing(path)

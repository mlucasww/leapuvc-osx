import pytest

from leapmac import bringup, uvc_control
from leapmac.uvc_descriptors import parse_configuration


class FakeDevice:
    def __init__(self, fail_at=()):
        self.calls = []
        self.fail_at = set(fail_at)

    def write_raw(self, *arguments):
        position = len(self.calls)
        self.calls.append(arguments)
        if position in self.fail_at:
            raise uvc_control.LibusbError(-9, "LIBUSB_ERROR_PIPE", "raw control write")


def test_parse_reads_hex_fields_and_data():
    rows = bringup.parse_sequence("# comment\n\n0x21 0x01 0x0b00 0x0200 4d000000\n0x01 0x0b 0x0000 0x0001 -\n")
    assert rows == [
        bringup.ControlWrite(0x21, 0x01, 0x0B00, 0x0200, bytes([0x4D, 0, 0, 0])),
        bringup.ControlWrite(0x01, 0x0B, 0x0000, 0x0001, b""),
    ]


def test_parse_rejects_short_rows():
    with pytest.raises(ValueError):
        bringup.parse_sequence("0x21 0x01 0x0b00\n")


def test_parse_rejects_bad_hex_rather_than_sending_zero():
    with pytest.raises(ValueError):
        bringup.parse_sequence("0x21 0x01 0x0b00 0x0200 zz\n")


def test_bundled_sequence_matches_the_recording():
    rows = bringup.load_sequence()
    assert len(rows) == 358
    assert rows[0] == bringup.ControlWrite(0x21, 0x01, 0x0B00, 0x0200, bytes([0x4D, 0, 0, 0]))
    assert rows[-1].value == 0x0400


def test_reset_split_lands_on_the_stray_set_interface():
    row = bringup.load_sequence()[bringup.RESET_SPLIT]
    assert (row.request_type, row.request, row.index) == (0x01, 0x0B, 0x0001)


def test_bundled_sequence_commits_to_the_streaming_interface():
    rows = bringup.load_sequence()
    targets = [row for row in rows if row.value in (0x0100, 0x0200) and row.index == 0x0001]
    assert [row.value for row in targets] == [0x0100, 0x0200]
    assert all(len(row.data) == 26 for row in targets)


def test_bundled_sequence_never_touches_vertical_zoom():
    contrast = [row for row in bringup.load_sequence() if row.value == 0x0300 and row.index == 0x0500]
    assert all((int.from_bytes(row.data, "little") & 0x3F) != 6 for row in contrast)


def test_replay_sends_rows_in_order():
    rows = bringup.parse_sequence("0x21 0x01 0x0b00 0x0200 01000000\n0x01 0x0b 0x0000 0x0001 -\n")
    device = FakeDevice()

    outcome = bringup.replay(device, rows)

    assert outcome == {"attempted": 2, "failed": 0, "failures": []}
    assert device.calls == [(0x21, 0x01, 0x0B00, 0x0200, bytes([1, 0, 0, 0])), (0x01, 0x0B, 0, 1, b"")]


def test_replay_continues_past_a_failed_row():
    rows = bringup.parse_sequence(
        "0x21 0x01 0x0b00 0x0200 01000000\n0x01 0x0b 0x0000 0x0001 -\n0x21 0x01 0x0400 0x0500 10000000\n"
    )
    device = FakeDevice(fail_at=[1])

    outcome = bringup.replay(device, rows)

    assert len(device.calls) == 3
    assert outcome["failed"] == 1
    assert outcome["failures"][0]["position"] == 1
    assert "LIBUSB_ERROR_PIPE" in outcome["failures"][0]["error"]


def test_parser_finds_the_streaming_interface():
    interface = lambda number, subclass: bytes([9, 0x04, number, 0, 0, 0x0E, subclass, 0, 0])
    topology = parse_configuration(interface(0, 1) + interface(1, 2))
    assert topology.vc_interface == 0
    assert topology.vs_interface == 1

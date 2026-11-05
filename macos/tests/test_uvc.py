from leapmac import uvc_control
from leapmac.uvc_descriptors import parse_configuration


def interface(number, cls, subclass):
    return bytes([9, 0x04, number, 0, 0, cls, subclass, 0, 0])


def input_terminal(terminal_id, terminal_type=0x0201):
    return bytes([18, 0x24, 0x02, terminal_id]) + terminal_type.to_bytes(2, "little") + bytes(12)


def processing_unit(unit_id):
    return bytes([11, 0x24, 0x05, unit_id]) + bytes(7)


def extension_unit(unit_id):
    return bytes([8, 0x24, 0x06, unit_id]) + bytes(4)


def uncompressed_format(index, fourcc):
    guid = fourcc.encode("ascii") + bytes(12)
    return bytes([27, 0x24, 0x04, index, 1]) + guid + bytes([16]) + bytes(5)


def uncompressed_frame(width, height, interval):
    body = bytes([0, 0]) + width.to_bytes(2, "little") + height.to_bytes(2, "little")
    body += bytes(12) + interval.to_bytes(4, "little") + bytes([1]) + interval.to_bytes(4, "little")
    return bytes([30, 0x24, 0x05]) + body


def leap_like_configuration():
    return b"".join(
        [
            bytes([9, 0x02, 0, 0, 2, 1, 0, 0x80, 250]),
            interface(0, 0x0E, 0x01),
            input_terminal(1),
            processing_unit(2),
            extension_unit(3),
            interface(1, 0x0E, 0x02),
            uncompressed_format(1, "YUY2"),
            uncompressed_frame(640, 480, 200000),
            uncompressed_frame(640, 240, 100000),
        ]
    )


def test_descriptor_helpers_build_spec_lengths():
    assert len(uncompressed_format(1, "YUY2")) == 27
    assert len(uncompressed_frame(640, 480, 200000)) == 30


def test_parse_finds_units_and_formats():
    topology = parse_configuration(leap_like_configuration())

    assert topology.is_uvc
    assert topology.vc_interface == 0
    assert topology.camera_terminal == 1
    assert topology.processing_unit == 2
    assert topology.extension_units == [3]

    assert len(topology.formats) == 1
    video = topology.formats[0]
    assert video.fourcc == "YUY2"
    assert video.bits_per_pixel == 16
    assert [(frame.width, frame.height) for frame in video.frames] == [(640, 480), (640, 240)]
    assert video.frames[0].default_interval_100ns == 200000
    assert video.frames[0].intervals_100ns == [200000]


def test_parse_ignores_non_camera_input_terminals():
    blob = interface(0, 0x0E, 0x01) + input_terminal(7, terminal_type=0x0101)
    assert parse_configuration(blob).camera_terminal is None


def test_parse_reports_non_uvc_devices():
    assert not parse_configuration(interface(0, 0x03, 0x01)).is_uvc


def test_parse_stops_safely_on_truncated_data():
    blob = leap_like_configuration()
    assert parse_configuration(blob[:-5]).is_uvc
    assert parse_configuration(bytes([0, 0, 0])).vc_interface is None


def device_with_units():
    device = uvc_control.UvcDevice.__new__(uvc_control.UvcDevice)
    device.topology = parse_configuration(leap_like_configuration())
    return device


def test_exposure_targets_the_camera_terminal_zoom_control():
    value, index = device_with_units()._control_address("exposure")
    assert value == 0x0B << 8
    assert index == (1 << 8) | 0


def test_calibration_controls_target_the_processing_unit():
    device = device_with_units()
    assert device._control_address("sharpness") == (0x08 << 8, 2 << 8)
    assert device._control_address("saturation") == (0x07 << 8, 2 << 8)


def test_vertical_zoom_is_not_exposed():
    constants = [name for name in dir(uvc_control) if name.startswith("CONTRAST_")]
    assert all(getattr(uvc_control, name) != 6 for name in constants)


def test_calibration_covers_exactly_156_addresses():
    assert len(uvc_control.CALIBRATION_ADDRESSES) == 156

import math
import struct

import pytest

from leapmac import calibration_blob


def camera_values(focal=130.0, base=0.0):
    return [
        focal,          # focal length
        base + 1, base + 2,  # offset
        base + 3, base + 4,  # tangential
        base + 5, base + 6, base + 7, base + 8, base + 9, base + 10,  # radial
        999.0,          # deprecated focal length
        base + 11, base + 12,  # centre
        base + 13, base + 14, base + 15,  # rotation
    ]


def make_blob(baseline=40.0, left_focal=130.0, right_focal=131.0):
    floats = [baseline, 0.5] + camera_values(left_focal, 0.0) + camera_values(right_focal, 20.0)
    return struct.pack("<BBBBI" + "f" * 36 + "I", 1, 2, 3, 4, 1000, *floats, 7777)


def test_blob_is_156_bytes():
    assert len(make_blob()) == calibration_blob.BLOB_SIZE == 156


def test_parse_maps_header_fields():
    parsed = calibration_blob.parse_blob(make_blob())
    assert parsed["signature"] == (1, 2)
    assert parsed["version"] == 3
    assert parsed["score"] == 4
    assert parsed["timestamp"] == 1000
    assert parsed["baseline"] == pytest.approx(40.0)
    assert parsed["q2init"] == pytest.approx(0.5)
    assert parsed["checksum"] == 7777


def test_parse_keeps_left_and_right_blocks_separate():
    parsed = calibration_blob.parse_blob(make_blob())
    left, right = parsed["left"], parsed["right"]

    assert left["focal_length"] == pytest.approx(130.0)
    assert left["offset"] == pytest.approx((1, 2))
    assert left["tangential"] == pytest.approx((3, 4))
    assert left["radial"] == pytest.approx((5, 6, 7, 8, 9, 10))
    assert left["deprecated_focal_length"] == pytest.approx(999.0)
    assert left["center"] == pytest.approx((11, 12))
    assert left["rotation"] == pytest.approx((13, 14, 15))

    assert right["focal_length"] == pytest.approx(131.0)
    assert right["tangential"] == pytest.approx((23, 24))
    assert right["rotation"] == pytest.approx((33, 34, 35))


def test_wrong_length_is_rejected():
    with pytest.raises(ValueError):
        calibration_blob.parse_blob(bytes(155))


@pytest.mark.parametrize("baseline", [0.04, 4.0, 40.0])
def test_baseline_is_plausible_in_any_common_unit(baseline):
    parsed = calibration_blob.parse_blob(make_blob(baseline=baseline))
    assert calibration_blob.plausibility_problems(parsed) == []


def test_unusual_values_are_reported():
    parsed = calibration_blob.parse_blob(make_blob(baseline=500.0, left_focal=5.0))
    problems = calibration_blob.plausibility_problems(parsed)
    assert any("baseline" in problem for problem in problems)
    assert any("left: focal length" in problem for problem in problems)


def test_non_finite_values_are_reported():
    parsed = calibration_blob.parse_blob(make_blob(left_focal=math.nan))
    assert "left: non-finite lens values" in calibration_blob.plausibility_problems(parsed)

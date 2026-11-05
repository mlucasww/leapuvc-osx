"""Decode the Leap's 156-byte factory calibration blob.

The layout comes from `retrieveLeapCalibration` in Python/leapuvc.py. This
module only reads raw fields. Turning them into OpenCV camera models is a
Phase 1 job, shared with the existing Windows and Linux code.
"""

import math
import struct

BLOB_SIZE = 156
_FORMAT = "<BBBBI" + "f" * 36 + "I"
_CAMERA_FIELDS = 17


def _camera(values):
    return {
        "focal_length": values[0],
        "offset": values[1:3],
        "tangential": values[3:5],
        "radial": values[5:11],
        "deprecated_focal_length": values[11],
        "center": values[12:14],
        "rotation": values[14:17],
    }


def parse_blob(blob):
    """Split the blob into named raw fields."""
    if len(blob) != BLOB_SIZE:
        raise ValueError(f"calibration blob must be {BLOB_SIZE} bytes, got {len(blob)}")
    values = struct.unpack(_FORMAT, blob)
    cameras_start = 7
    right_start = cameras_start + _CAMERA_FIELDS
    return {
        "signature": values[0:2],
        "version": values[2],
        "score": values[3],
        "timestamp": values[4],
        "baseline": values[5],
        "q2init": values[6],
        "left": _camera(values[cameras_start:right_start]),
        "right": _camera(values[right_start : right_start + _CAMERA_FIELDS]),
        "checksum": values[-1],
    }


def _baseline_is_plausible(baseline):
    """The baseline's unit is undocumented, so accept about 40 mm in m, cm or mm."""
    return any(low <= baseline <= high for low, high in ((0.03, 0.05), (3, 5), (30, 50)))


def plausibility_problems(parsed):
    """Return a list of reasons the blob looks wrong. An empty list means plausible."""
    problems = []
    for name in ("left", "right"):
        camera = parsed[name]
        numbers = (
            [camera["focal_length"]]
            + list(camera["offset"])
            + list(camera["tangential"])
            + list(camera["radial"])
        )
        if not all(math.isfinite(number) for number in numbers):
            problems.append(f"{name}: non-finite lens values")
            continue
        if not 50 <= camera["focal_length"] <= 1000:
            problems.append(f"{name}: focal length {camera['focal_length']:.1f} px is unusual")
        if max(abs(value) for value in camera["offset"]) > 100:
            problems.append(f"{name}: principal point offset {camera['offset']} is unusual")
    baseline = parsed["baseline"]
    if not math.isfinite(baseline) or not _baseline_is_plausible(baseline):
        problems.append(f"baseline {baseline} is unusual")
    return problems

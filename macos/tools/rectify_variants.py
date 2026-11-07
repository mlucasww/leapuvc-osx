#!/usr/bin/env python3
"""Score alternative readings of the factory geometry against tape-measured runs.

Phase 2's first measurements read about 12% too far, with rows 0.6 to 0.8 px
apart. The suspects are the rotation convention and the unused "center"
values. This re-rectifies the raw frames that depth_test.py saved, under each
reading, and compares the result with the measured distance. It needs no
camera:

    .venv/bin/python tools/rectify_variants.py
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from leapmac import calibration_blob, calibration_cache, capture, stereo  # noqa: E402

RESOLUTION = (640, 480)


def center_offsets(blob):
    parsed = calibration_blob.parse_blob(blob)
    return {camera: tuple(parsed[camera]["center"]) for camera in ("left", "right")}


def variants(blob):
    centers = center_offsets(blob)
    return {
        "factory": {"rotation": "factory"},
        "transposed": {"rotation": "transposed"},
        "identity": {"rotation": "identity"},
        "factory+centers": {"rotation": "factory", "offsets": centers},
        "transposed+centers": {"rotation": "transposed", "offsets": centers},
    }


def score(rig, frames, true_m, half_size):
    shifts = []
    for frame in frames:
        left, right = rig.rectify(frame[0], frame[1])
        shifts.append(stereo.region_shift(left, right, half_size))
    disparity = float(np.median([shift["disparity_px"] for shift in shifts]))
    expected = rig.fx * rig.baseline_m / true_m
    depth = float(rig.depth(disparity)) if disparity > 0 else None
    return {
        "true_m": true_m,
        "disparity_px": round(disparity, 3),
        "expected_px": round(expected, 3),
        "deficit_px": round(expected - disparity, 3),
        "depth_m": round(depth, 4) if depth else None,
        "error_percent": round((depth - true_m) / true_m * 100, 2) if depth else None,
        "row_offset_px": round(float(np.median([shift["row_offset_px"] for shift in shifts])), 3),
        "response": round(float(np.median([shift["response"] for shift in shifts])), 3),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--log", type=Path, default=ROOT / "probe_out" / "depth-log.jsonl")
    parser.add_argument("--calibration", type=Path, help="a calibration .bin, if not the only cached one")
    args = parser.parse_args()

    cached = sorted(calibration_cache.DEFAULT_DIRECTORY.glob("*.bin"))
    path = args.calibration or (cached[0] if len(cached) == 1 else None)
    if path is None:
        sys.exit("Pass --calibration: there isn't exactly one cached calibration")
    blob = path.read_bytes()
    calibration = capture.parseLeapCalibration(blob, RESOLUTION)

    records = [json.loads(line) for line in args.log.read_text().splitlines() if line.strip()]
    runs = [record for record in records if (Path(record["images"]) / "raw.npz").exists()]
    if not runs:
        sys.exit("No runs with raw.npz yet. Run depth_test.py first.")

    results = {}
    for name, options in variants(blob).items():
        rig = stereo.StereoRig.from_calibration(calibration, RESOLUTION, **options)
        rows = []
        for record in runs:
            frames = np.load(Path(record["images"]) / "raw.npz")["frames"]
            rows.append(score(rig, frames, record["true_m"], record["region_half_size"]))
        errors = [abs(row["error_percent"]) for row in rows if row["error_percent"] is not None]
        results[name] = {
            "runs": rows,
            "mean_abs_error_percent": round(float(np.mean(errors)), 2) if errors else None,
            "mean_abs_row_offset_px": round(float(np.mean([abs(row["row_offset_px"]) for row in rows])), 3),
        }

    print(f"{'variant':<20} {'mean |error| %':>15} {'mean |row offset| px':>21}")
    for name, result in results.items():
        print(f"{name:<20} {str(result['mean_abs_error_percent']):>15} {result['mean_abs_row_offset_px']:>21}")

    out = ROOT / "probe_out" / f"variants-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps({"calibration": str(path), "results": results}, indent=2))
    print(f"Report: {out}")


if __name__ == "__main__":
    main()

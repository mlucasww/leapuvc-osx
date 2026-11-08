#!/usr/bin/env python3
"""Score alternative readings of the factory geometry against tape-measured runs.

Phase 2's first measurements read too far, with rows out of line. The suspects
are the rotation convention and the unused "center" values. This re-rectifies
the raw frames that depth_test.py saved, under each reading, and compares the
result with the measured distance. It needs no camera:

    .venv/bin/python tools/rectify_variants.py
    .venv/bin/python tools/rectify_variants.py --images depth-20261107-155131 depth-20261107-155252

Disparity comes from SGBM, averaged over the measured region. Row offset comes
from correlating a larger window, and only counts when the match is strong.
A straight-line fit across distances separates a scale error from an offset.
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
SHIFT_HALF_SIZE = 64
MIN_SHIFT_RESPONSE = 0.3


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


def score(rig, matcher, frames, record):
    disparities, rows = [], []
    for frame in frames:
        left, right = rig.rectify(frame[0], frame[1])
        value = stereo.region_disparity(stereo.compute_disparity(matcher, left, right), record["region_half_size"])
        if value is not None:
            disparities.append(value)
        shift = stereo.region_shift(left, right, SHIFT_HALF_SIZE)
        if shift["response"] >= MIN_SHIFT_RESPONSE:
            rows.append(shift["row_offset_px"])

    true_m = record["true_m"]
    expected = rig.fx * rig.baseline_m / true_m
    result = {
        "images": Path(record["images"]).name,
        "true_m": true_m,
        "expected_px": round(expected, 3),
        "row_offset_px": round(float(np.median(rows)), 3) if rows else None,
        "row_samples": len(rows),
    }
    if not disparities:
        return result | {"disparity_px": None}
    disparity = float(np.median(disparities))
    depth = rig.depth(disparity)
    return result | {
        "disparity_px": round(disparity, 3),
        "deficit_px": round(expected - disparity, 3),
        "depth_m": round(float(depth), 4),
        "error_percent": round(float((depth - true_m) / true_m * 100), 2),
    }


def summarise(rows):
    scored = [row for row in rows if row.get("disparity_px") is not None]
    offsets = [abs(row["row_offset_px"]) for row in rows if row["row_offset_px"] is not None]
    fit = stereo.fit_scale_offset(
        [row["expected_px"] for row in scored], [row["disparity_px"] for row in scored]
    )
    offset_only = stereo.fit_offset(
        [row["expected_px"] for row in scored], [row["disparity_px"] for row in scored]
    )
    return {
        "fit": fit,
        "offset_only": offset_only,
        "median_abs_error_percent": round(float(np.median([abs(row["error_percent"]) for row in scored])), 2)
        if scored
        else None,
        "median_abs_row_offset_px": round(float(np.median(offsets)), 3) if offsets else None,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--log", type=Path, default=ROOT / "probe_out" / "depth-log.jsonl")
    parser.add_argument("--calibration", type=Path, help="a calibration .bin, if not the only cached one")
    parser.add_argument("--images", nargs="*", help="only these run folders, by name")
    args = parser.parse_args()

    cached = sorted(calibration_cache.DEFAULT_DIRECTORY.glob("*.bin"))
    path = args.calibration or (cached[0] if len(cached) == 1 else None)
    if path is None:
        sys.exit("Pass --calibration: there isn't exactly one cached calibration")
    blob = path.read_bytes()
    calibration = capture.parseLeapCalibration(blob, RESOLUTION)

    records = [json.loads(line) for line in args.log.read_text().splitlines() if line.strip()]
    runs = [record for record in records if (Path(record["images"]) / "raw.npz").exists()]
    if args.images:
        wanted = set(args.images)
        runs = [record for record in runs if Path(record["images"]).name in wanted]
    if not runs:
        sys.exit("No matching runs with raw.npz.")

    matcher = stereo.make_matcher()
    results = {}
    for name, options in variants(blob).items():
        rig = stereo.StereoRig.from_calibration(calibration, RESOLUTION, **options)
        rows = []
        for record in runs:
            frames = np.load(Path(record["images"]) / "raw.npz")["frames"]
            rows.append(score(rig, matcher, frames, record))
        results[name] = {"summary": summarise(rows), "runs": rows}

    print(
        f"{'variant':<20} {'scale':>7} {'offset px':>10} {'offset only':>12} {'spread':>7} "
        f"{'median |err| %':>15} {'median |row| px':>16}"
    )
    for name, result in results.items():
        summary = result["summary"]
        fit = summary["fit"] or {}
        only = summary["offset_only"] or {}
        scale = f"{fit['scale']:.3f}" if fit else "-"
        offset = f"{fit['offset_px']:.3f}" if fit else "-"
        offset_only = f"{only['offset_px']:.3f}" if only else "-"
        spread = f"{only['spread_px']:.3f}" if only else "-"
        print(
            f"{name:<20} {scale:>7} {offset:>10} {offset_only:>12} {spread:>7} "
            f"{str(summary['median_abs_error_percent']):>15} {str(summary['median_abs_row_offset_px']):>16}"
        )

    out = ROOT / "probe_out" / f"variants-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps({"calibration": str(path), "runs_scored": len(runs), "results": results}, indent=2))
    print(f"Scored {len(runs)} run(s). Report: {out}")


if __name__ == "__main__":
    main()

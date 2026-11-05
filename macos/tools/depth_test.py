#!/usr/bin/env python3
"""Tape-measure test: compare the Leap's depth with a measured distance.

Place a flat, textured target square-on, centred in front of the Leap. Measure
from the Leap's front face to the target, then run from Terminal.app, in the
macos/ folder:

    .venv/bin/python tools/depth_test.py --distance 1.0

Add --preview to aim first. A window shows the image and depth, with a box
around the measured region. Press space to measure, or q to cancel.

Each run adds to probe_out/depth-log.jsonl. To print the error table:

    .venv/bin/python tools/depth_test.py --summary
"""

import argparse
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from leapmac import capture, stereo  # noqa: E402

RESOLUTION = (640, 480)
SETTLE_S = 1.0
FRAME_TIMEOUT_S = 3.0


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--distance", type=float, help="measured distance to the target, in metres")
    parser.add_argument("--frames", type=int, default=30)
    parser.add_argument("--region", type=int, default=40, help="half-size of the measured square, in pixels")
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--note", default="")
    parser.add_argument("--out", type=Path, default=ROOT / "probe_out")
    args = parser.parse_args()
    if not args.summary and args.distance is None:
        parser.error("give --distance, or --summary")
    return args


def next_image(leap):
    deadline = time.monotonic() + FRAME_TIMEOUT_S
    while time.monotonic() < deadline:
        fresh, image = leap.read()
        if fresh:
            return image
        time.sleep(0.002)
    sys.exit("No frame arrived")


def colorize(disparity, num_disparities):
    scaled = np.nan_to_num(disparity, nan=0.0) / num_disparities * 255
    colored = cv2.applyColorMap(scaled.clip(0, 255).astype(np.uint8), cv2.COLORMAP_TURBO)
    colored[~np.isfinite(disparity)] = 0
    return colored


def draw_region(image, half_size):
    height, width = image.shape[:2]
    corner_a = (width // 2 - half_size, height // 2 - half_size)
    corner_b = (width // 2 + half_size, height // 2 + half_size)
    cv2.rectangle(image, corner_a, corner_b, (255, 255, 255), 1)


def preview(leap, rig, matcher, args):
    """Show the live view. Returns True to measure, or False to cancel."""
    print("Aim at the target. Press space to measure, or q to cancel.")
    while leap.running:
        image = next_image(leap)
        left, right = rig.rectify(image[0], image[1])
        disparity = stereo.compute_disparity(matcher, left, right)
        stats = stereo.region_stats(rig.depth(disparity), args.region)

        view = np.hstack((cv2.cvtColor(left, cv2.COLOR_GRAY2BGR), colorize(disparity, stereo.DEFAULT_NUM_DISPARITIES)))
        draw_region(view, args.region)
        if stats["median_m"] is not None:
            label = f"{stats['median_m']:.3f} m, {stats['valid_fraction']:.0%} valid"
            cv2.putText(view, label, (12, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
        cv2.imshow("Leap depth", view)

        key = cv2.waitKey(1) & 0xFF
        if key == ord(" "):
            cv2.destroyAllWindows()
            return True
        if key == ord("q"):
            cv2.destroyAllWindows()
            return False
    return False


def measure(leap, rig, matcher, args):
    time.sleep(SETTLE_S)
    medians, valid_fractions, match_times = [], [], []
    for _ in range(args.frames):
        image = next_image(leap)
        left, right = rig.rectify(image[0], image[1])
        started = time.perf_counter()
        disparity = stereo.compute_disparity(matcher, left, right)
        match_times.append(time.perf_counter() - started)

        stats = stereo.region_stats(rig.depth(disparity), args.region)
        valid_fractions.append(stats["valid_fraction"])
        if stats["median_m"] is not None:
            medians.append(stats["median_m"])

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    directory = args.out / f"depth-{stamp}"
    directory.mkdir(parents=True)
    cv2.imwrite(str(directory / "rectified_left.png"), left)
    cv2.imwrite(str(directory / "rectified_right.png"), right)
    colored = colorize(disparity, stereo.DEFAULT_NUM_DISPARITIES)
    draw_region(colored, args.region)
    cv2.imwrite(str(directory / "disparity.png"), colored)

    return {
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
        "true_m": args.distance,
        "note": args.note,
        "frames": args.frames,
        "region_half_size": args.region,
        "median_m": float(np.median(medians)) if medians else None,
        "frame_std_m": float(statistics.pstdev(medians)) if len(medians) > 1 else None,
        "valid_fraction": float(np.median(valid_fractions)),
        "match_ms": round(float(np.median(match_times)) * 1000, 1),
        "alignment": stereo.vertical_alignment(left, right),
        "expected_error_m": rig.expected_error_m(args.distance),
        "images": str(directory),
    }


def print_summary(path):
    if not path.exists():
        sys.exit(f"No measurements yet in {path}")
    records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    print(f"{'true m':>7} {'runs':>5} {'measured':>9} {'error m':>8} {'error %':>8} {'noise m':>8} {'valid':>6}")
    for row in stereo.summarise_measurements(records):
        if row["measured_m"] is None:
            print(f"{row['true_m']:>7.2f} {row['runs']:>5} {'no depth':>9}")
            continue
        print(
            f"{row['true_m']:>7.2f} {row['runs']:>5} {row['measured_m']:>9.3f} {row['error_m']:>8.3f} "
            f"{row['error_percent']:>8.1f} {row['frame_noise_m']:>8.4f} {row['valid_fraction']:>6.0%}"
        )


def main():
    args = parse_args()
    log_path = args.out / "depth-log.jsonl"
    if args.summary:
        print_summary(log_path)
        return

    leap = capture.leapImageThread(resolution=RESOLUTION, timeout=30)
    rig = stereo.StereoRig.from_calibration(leap.calibration, leap.resolution)
    matcher = stereo.make_matcher()
    leap.start()
    try:
        if args.preview and not preview(leap, rig, matcher, args):
            return
        record = measure(leap, rig, matcher, args)
    finally:
        leap.stop()
        leap.join(timeout=10)

    args.out.mkdir(parents=True, exist_ok=True)
    with log_path.open("a") as log:
        log.write(json.dumps(record) + "\n")
    print(json.dumps(record, indent=2))


if __name__ == "__main__":
    main()

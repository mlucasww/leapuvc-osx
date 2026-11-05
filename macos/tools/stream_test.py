#!/usr/bin/env python3
"""Soak test: stream through the full capture path and count every frame.

This checks Phase 1's exit conditions: no torn frames, no skipped frames and a
steady frame rate. It also logs consecutive frame labels, which Phase 0 could
not check because it only sampled even frames.

Run it from Terminal.app, from the macos/ folder:

    .venv/bin/python tools/stream_test.py --seconds 600
"""

import argparse
import json
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from leapmac import capture  # noqa: E402

LABELS_KEPT = 240
SAMPLE_EVERY_FRAMES = 500
REPORT_EVERY_S = 30


class FrameLog:
    """Collects per-frame facts on the capture thread."""

    def __init__(self):
        self.labels = []
        self.label_counts = Counter()
        self.exposures = Counter()
        self.gains = Counter()
        self.samples = []
        self.previous_pts = None
        self.pts_gaps_ms = Counter()
        self.largest_gap_ms = 0.0
        self.frames = 0

    def __call__(self, sequence, pts_ns, embedded, image):
        label, exposure, gain = embedded
        self.frames += 1
        self.label_counts[label] += 1
        self.exposures[exposure] += 1
        self.gains[gain] += 1
        if len(self.labels) < LABELS_KEPT:
            self.labels.append(label)

        if self.previous_pts is not None and pts_ns > self.previous_pts:
            gap_ms = (pts_ns - self.previous_pts) / 1e6
            self.pts_gaps_ms[round(gap_ms)] += 1
            self.largest_gap_ms = max(self.largest_gap_ms, gap_ms)
        self.previous_pts = pts_ns

        if self.frames % SAMPLE_EVERY_FRAMES == 1:
            self.samples.append(
                {
                    "sequence": sequence,
                    "left_mean": round(float(image[0].mean()), 2),
                    "right_mean": round(float(image[1].mean()), 2),
                }
            )


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seconds", type=float, default=600)
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--out", type=Path, default=ROOT / "probe_out")
    return parser.parse_args()


def main():
    args = parse_args()
    opening = time.monotonic()
    leap = capture.leapImageThread(resolution=(args.width, args.height))
    startup_seconds = round(time.monotonic() - opening, 3)

    log = FrameLog()
    leap.frameListener = log
    leap.start()

    started = time.monotonic()
    next_report = started + REPORT_EVERY_S
    while time.monotonic() - started < args.seconds and leap.running:
        time.sleep(0.05)
        leap.read()  # This also resets the thread's idle timeout.
        if time.monotonic() >= next_report:
            next_report += REPORT_EVERY_S
            elapsed = time.monotonic() - started
            print(
                f"{elapsed:6.0f} s  {leap.framesReceived} frames  "
                f"{leap.framesReceived / elapsed:.2f} FPS  skipped {leap.framesSkipped}  "
                f"torn {leap.ring.torn}"
            )

    finished_early = time.monotonic() - started < args.seconds
    elapsed = time.monotonic() - started
    leap.stop()
    leap.join(timeout=10)

    done = leap.helper.event("done") or {}
    torn = getattr(leap, "torn", None)
    published = done.get("frames_published", 0)
    report = {
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
        "resolution": [args.width, args.height],
        "seconds_requested": args.seconds,
        "seconds_run": round(elapsed, 1),
        "finished_early": finished_early,
        "startup_seconds": startup_seconds,
        "calibration_source": leap.calibrationSource,
        "frames_received": leap.framesReceived,
        "frames_skipped_by_reader": leap.framesSkipped,
        # Frames outside the reader's lifetime are expected, and aren't losses.
        "frames_before_reader_started": max(0, leap.firstSequence - 1),
        "frames_after_reader_stopped": max(0, published - leap.lastSequence),
        "torn_frames": torn,
        "helper_summary": done,
        "largest_frame_gap_ms": round(log.largest_gap_ms, 1),
        "frame_gap_histogram_ms": {str(key): log.pts_gaps_ms[key] for key in sorted(log.pts_gaps_ms)},
        "frame_label_counts": {str(key): value for key, value in sorted(log.label_counts.items())},
        "first_frame_labels": log.labels,
        "exposure_counts": {str(key): value for key, value in sorted(log.exposures.items())},
        "gain_counts": {str(key): value for key, value in sorted(log.gains.items())},
        "brightness_samples": log.samples,
    }

    clean = (
        not finished_early
        and torn == 0
        and leap.framesSkipped == 0
        and done.get("frames_dropped") == 0
    )
    report["clean"] = clean

    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"stream-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(report, indent=2))

    print()
    print("CLEAN RUN" if clean else "Problems found")
    print(f"{leap.framesReceived} frames in {elapsed:.1f} s, {done.get('measured_fps', 0):.2f} FPS")
    print(f"Skipped {leap.framesSkipped}, torn {torn}, dropped {done.get('frames_dropped')}")
    print(f"Largest frame gap {log.largest_gap_ms:.1f} ms, frame labels {dict(log.label_counts)}")
    print(f"Report: {path}")


if __name__ == "__main__":
    main()

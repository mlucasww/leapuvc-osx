#!/usr/bin/env python3
"""Show the Leap's live point cloud in the Rerun viewer.

Run from Terminal.app, in the macos/ folder:

    .venv/bin/python tools/live_cloud.py

The viewer opens with the 3D cloud, the rectified left image, the depth image,
and rate and timing plots. Press Ctrl-C in Terminal to stop. Each run writes a
report to probe_out/cloud-<timestamp>.json, which says whether capture stayed
clean while stereo ran alongside it.

Without a Leap, replay frames saved by depth_test.py instead, at the Leap's
57.5 FPS. To measure the pipeline's throughput, add --replay-fps 0 --rate 0:

    .venv/bin/python tools/live_cloud.py --replay probe_out/depth-20261107-155252/raw.npz --save cloud.rrd --seconds 10
"""

import argparse
import json
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import rerun as rr
import rerun.blueprint as rrb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from leapmac import calibration_cache, capture, pointcloud, stereo  # noqa: E402

RESOLUTION = (640, 480)
# The Leap advertises 57.5 FPS at 640x480 and measures 57.55 when healthy. A
# Leap can also stream steadily but slowly, at 42 to 53 FPS, until replugged,
# with nothing lost on the way, so a clean run must reach this rate too.
MIN_HEALTHY_FPS = 57.0
REPORT_EVERY_S = 5.0
FRAME_TIMEOUT_S = 3.0
POINT_RADIUS_UI = 1.5
# Timing statistics leave out the first second, while caches and the viewer
# warm up.
WARMUP_S = 1.0


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seconds", type=float, default=0, help="stop after this long; 0 runs until Ctrl-C")
    parser.add_argument("--replay", type=Path, help="a raw.npz saved by depth_test.py, instead of a live Leap")
    parser.add_argument("--calibration", type=Path, help="calibration .bin for --replay, if not the only cached one")
    parser.add_argument(
        "--replay-fps", type=float, default=57.5, help="frame rate to replay at; 0 replays as fast as possible"
    )
    parser.add_argument("--save", type=Path, help="write an .rrd file instead of opening the viewer")
    parser.add_argument(
        "--no-viewer", action="store_true", help="build clouds but send them nowhere, to measure stereo's load alone"
    )
    parser.add_argument(
        "--single-thread",
        action="store_true",
        help="run OpenCV on one thread; its macOS GCD backend can't limit threads any other way",
    )
    parser.add_argument("--exposure", type=int, help="exposure in microseconds")
    parser.add_argument("--gain", type=int, help="analog gain, 16 to 63")
    parser.add_argument("--digital-gain", type=int, help="digital gain, 4 to 15")
    parser.add_argument(
        "--disparity-offset",
        type=float,
        default=0.0,
        help="this unit's fitted disparity offset in px; the Phase 2 test unit measured about 0.17",
    )
    parser.add_argument("--min-depth", type=float, default=pointcloud.DEFAULT_MIN_DEPTH_M, help="metres")
    parser.add_argument("--max-depth", type=float, default=pointcloud.DEFAULT_MAX_DEPTH_M, help="metres")
    parser.add_argument(
        "--rate", type=float, default=30, help="most clouds per second to build and show; 0 for every frame"
    )
    parser.add_argument("--stride", type=int, default=2, help="keep every nth pixel in each direction")
    parser.add_argument("--num-disparities", type=int, default=stereo.DEFAULT_NUM_DISPARITIES)
    parser.add_argument("--out", type=Path, default=ROOT / "probe_out")
    return parser.parse_args()


def blueprint():
    return rrb.Blueprint(
        rrb.Horizontal(
            rrb.Spatial3DView(origin="world", name="Point cloud"),
            rrb.Vertical(
                rrb.Spatial2DView(origin="camera/left", name="Left, rectified"),
                rrb.Spatial2DView(origin="camera/depth", name="Depth"),
                rrb.TimeSeriesView(origin="stats/rate", name="Rates (per second)"),
                rrb.TimeSeriesView(origin="stats/ms", name="Processing time (ms)"),
            ),
            column_shares=[3, 1],
        ),
        collapse_panels=True,
    )


def start_rerun(args, rig):
    rr.init("leapmac_live_cloud")
    if args.save:
        rr.save(args.save, default_blueprint=blueprint())
    else:
        # Rerun looks for its viewer on PATH, which lacks the venv's bin
        # folder unless the venv is activated. Use the viewer installed
        # beside this Python instead, without resolving the venv's symlink.
        viewer = Path(sys.executable).parent / "rerun"
        rr.spawn(
            memory_limit="2GB",
            default_blueprint=blueprint(),
            executable_path=str(viewer) if viewer.exists() else None,
        )

    # The rig's frame is x right, y down, z forward.
    rr.log("world", rr.ViewCoordinates.RDF, static=True)
    rr.log(
        "world/leap",
        rr.Pinhole(
            focal_length=[rig.fx, rig.fy],
            principal_point=[rig.cx, rig.cy],
            resolution=[rig.width, rig.height],
            camera_xyz=rr.ViewCoordinates.RDF,
            image_plane_distance=0.05,
        ),
        static=True,
    )


def log_cloud(index, cloud, rates, max_depth_m):
    rr.set_time("frame", sequence=index)
    gray = cloud.brightness[:, None].repeat(3, axis=1)
    rr.log("world/points", rr.Points3D(cloud.points, colors=gray, radii=rr.Radius.ui_points(POINT_RADIUS_UI)))
    rr.log("camera/left", rr.Image(cloud.left, color_model="L"))
    # Millimetres in 16 bits: half the data of float metres, at finer steps
    # than the depth can resolve.
    millimetres = np.nan_to_num(cloud.depth * 1000.0, nan=0.0).astype(np.uint16)
    rr.log("camera/depth", rr.DepthImage(millimetres, meter=1000.0, depth_range=[0.0, max_depth_m * 1000.0]))
    for name, value in rates.items():
        rr.log(f"stats/rate/{name}", rr.Scalars(value))
    for name, value in cloud.timings_ms.items():
        rr.log(f"stats/ms/{name}", rr.Scalars(value))


class FrameGaps:
    """Track frame timing from the capture thread: the largest gap between
    timestamps, and the frame rate in each minute, so a slowdown shows when it
    started."""

    def __init__(self):
        self.first_pts_ns = None
        self.last_pts_ns = None
        self.largest_ms = 0.0
        self.per_minute = []

    def __call__(self, sequence, pts_ns, embedded_line, image):
        if self.first_pts_ns is None:
            self.first_pts_ns = pts_ns
        if self.last_pts_ns is not None:
            self.largest_ms = max(self.largest_ms, (pts_ns - self.last_pts_ns) / 1e6)
        self.last_pts_ns = pts_ns
        minute = int((pts_ns - self.first_pts_ns) // 60_000_000_000)
        while len(self.per_minute) <= minute:
            self.per_minute.append(0)
        self.per_minute[minute] += 1

    def fps_by_minute(self):
        """Frames per second in each minute. A final partial minute is left out."""
        if self.last_pts_ns is None:
            return []
        whole = int((self.last_pts_ns - self.first_pts_ns) // 60_000_000_000)
        return [round(count / 60, 2) for count in self.per_minute[:whole]]


class LiveSource:
    def __init__(self, args):
        self.leap = capture.leapImageThread(resolution=RESOLUTION, timeout=30)
        self.calibration = self.leap.calibration
        self.gaps = FrameGaps()
        self.leap.frameListener = self.gaps
        self.leap.start()
        if args.exposure is not None:
            self.leap.setExposure(args.exposure)
        if args.gain is not None:
            self.leap.setGain(args.gain)
        if args.digital_gain is not None:
            self.leap.setDigitalGain(args.digital_gain)

    @property
    def running(self):
        return self.leap.running

    @property
    def frames_received(self):
        return self.leap.framesReceived

    def next(self):
        deadline = time.monotonic() + FRAME_TIMEOUT_S
        while time.monotonic() < deadline and self.leap.running:
            fresh, image = self.leap.read()
            if fresh:
                return image
            time.sleep(0.001)
        return None

    def close(self):
        self.leap.stop()
        self.leap.join(timeout=10)
        done = self.leap.helper.event("done") or {}
        published = done.get("frames_published", 0)
        return {
            "frames_received": self.leap.framesReceived,
            "frames_skipped_by_reader": self.leap.framesSkipped,
            "frames_after_reader_stopped": max(0, published - self.leap.lastSequence),
            "torn_frames": getattr(self.leap, "torn", None),
            "frames_dropped_by_helper": done.get("frames_dropped"),
            "helper_fps": done.get("measured_fps"),
            "largest_frame_gap_ms": round(self.gaps.largest_ms, 1),
            "fps_by_minute": self.gaps.fps_by_minute(),
        }


class ReplaySource:
    """Loop over saved frames at the Leap's frame rate, or as fast as they're taken."""

    def __init__(self, args):
        self.frames = np.load(args.replay)["frames"]
        cached = sorted(calibration_cache.DEFAULT_DIRECTORY.glob("*.bin"))
        path = args.calibration or (cached[0] if len(cached) == 1 else None)
        if path is None:
            sys.exit("Pass --calibration: there isn't exactly one cached calibration")
        self.calibration = capture.parseLeapCalibration(path.read_bytes(), RESOLUTION)
        self.index = 0
        self.running = True
        self.interval_s = 1.0 / args.replay_fps if args.replay_fps else 0.0
        self.next_due = time.monotonic()

    @property
    def frames_received(self):
        return self.index

    def next(self):
        if self.interval_s:
            delay = self.next_due - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self.next_due = max(self.next_due + self.interval_s, time.monotonic() - self.interval_s)
        frame = self.frames[self.index % len(self.frames)]
        self.index += 1
        return frame

    def close(self):
        return {"frames_replayed": self.index}


def percentile(values, q):
    return round(float(np.percentile(values, q)), 2) if values else None


def main():
    args = parse_args()
    settings = pointcloud.CloudSettings(
        min_depth_m=args.min_depth,
        max_depth_m=args.max_depth,
        stride=args.stride,
        num_disparities=args.num_disparities,
    )
    if args.single_thread:
        cv2.setNumThreads(1)
    stopping = []
    signal.signal(signal.SIGINT, lambda *_: stopping.append(True))
    source = ReplaySource(args) if args.replay else LiveSource(args)

    capture_rate, cloud_rate = pointcloud.RateMeter(), pointcloud.RateMeter()
    totals, log_ms, points = [], [], []
    started = time.monotonic()
    counted_from = None
    clouds = 0
    last_received = 0
    # From here the capture thread is running, so any failure must stop it.
    try:
        rig = stereo.StereoRig.from_calibration(
            source.calibration, RESOLUTION, disparity_offset_px=args.disparity_offset
        )
        builder = pointcloud.CloudBuilder(rig, settings)
        if not args.no_viewer:
            start_rerun(args, rig)
        started = time.monotonic()
        next_report = started + REPORT_EVERY_S
        next_cloud = started

        while source.running and not stopping:
            elapsed = time.monotonic() - started
            if args.seconds and elapsed >= args.seconds:
                break
            frame = source.next()
            if frame is None:
                print("No frame arrived for 3 s; stopping.")
                break
            received = source.frames_received
            for _ in range(received - last_received):
                capture_rate.tick()
            last_received = received
            # Capture keeps every frame either way. Skipping some here only
            # spares the viewer, which receives about 0.8 MB per cloud.
            if args.rate and time.monotonic() < next_cloud:
                continue
            next_cloud = max(next_cloud + 1.0 / args.rate, time.monotonic()) if args.rate else 0

            cloud = builder.process(frame)
            logging_started = time.perf_counter()
            if not args.no_viewer:
                log_cloud(
                    clouds,
                    cloud,
                    {"capture": capture_rate.rate, "cloud": cloud_rate.rate},
                    args.max_depth,
                )
            cloud_rate.tick()
            clouds += 1

            if elapsed >= WARMUP_S:
                if counted_from is None:
                    counted_from = (time.monotonic(), clouds)
                totals.append(cloud.timings_ms["total"])
                log_ms.append((time.perf_counter() - logging_started) * 1000)
                points.append(cloud.size)

            if time.monotonic() >= next_report:
                next_report += REPORT_EVERY_S
                print(
                    f"{elapsed:6.0f} s  capture {capture_rate.rate:5.1f}/s  cloud {cloud_rate.rate:5.1f}/s  "
                    f"process {cloud.timings_ms['total']:5.1f} ms  points {cloud.size}"
                )
    finally:
        seconds_run = time.monotonic() - started
        capture_report = source.close()

    if args.replay:
        capture_report["replayed_from"] = str(args.replay)
    counted_s = time.monotonic() - counted_from[0] if counted_from else 0
    report = {
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "replay" if args.replay else "live",
        "seconds_run": round(seconds_run, 1),
        "settings": {
            "exposure": args.exposure,
            "gain": args.gain,
            "digital_gain": args.digital_gain,
            "disparity_offset_px": args.disparity_offset,
            "depth_range_m": [args.min_depth, args.max_depth],
            "stride": args.stride,
            "num_disparities": args.num_disparities,
            "viewer": not args.no_viewer,
            "single_thread": args.single_thread,
        },
        "clouds": clouds,
        "cloud_rate": round((clouds - counted_from[1]) / counted_s, 2) if counted_s else None,
        "process_ms": {"p50": percentile(totals, 50), "p95": percentile(totals, 95), "max": percentile(totals, 100)},
        "log_ms": {"p50": percentile(log_ms, 50), "p95": percentile(log_ms, 95)},
        "points": {"p50": percentile(points, 50), "max": percentile(points, 100)},
        "capture": capture_report,
    }
    if not args.replay:
        report["capture"]["fps"] = round(capture_report["frames_received"] / seconds_run, 2)
        report["capture_clean"] = (
            capture_report["frames_skipped_by_reader"] == 0
            and capture_report["torn_frames"] == 0
            and capture_report["frames_dropped_by_helper"] == 0
            and (capture_report["helper_fps"] or 0) >= MIN_HEALTHY_FPS
        )

    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"cloud-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    print(f"Report: {path}")


if __name__ == "__main__":
    main()

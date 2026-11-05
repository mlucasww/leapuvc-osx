#!/usr/bin/env python3
"""Find out whether a control-write bring-up makes the Leap send video on macOS.

The first probe showed macOS's driver reading the stream and the Leap sending
nothing. This replays OpenMotion's recorded sequence over endpoint 0, then
checks whether frames arrive through the normal AVFoundation helper.

Scenarios, to be tried one at a time:

  before  replay every row, then start capture
  during  start capture so reads are pending, then replay every row
  reset   replay up to the recorded reset, reset, replay the rest, then capture

Run it from Terminal.app, from the macos/ folder, with no root:

    .venv/bin/python tools/bringup_test.py before

Results go to probe_out/bringup-<scenario>-<timestamp>/.
"""

import argparse
import json
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import probe_leap  # noqa: E402
from leapmac import bringup, uvc_control  # noqa: E402

CAPTURE_LEAD_S = 1.5
FRAME_TIMEOUT_S = 10
SETTLE_S = 0.5
REOPEN_ATTEMPTS = 20
REOPEN_DELAY_S = 0.15


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("scenario", choices=("before", "during", "reset"))
    parser.add_argument("--vid", type=lambda text: int(text, 0), default=probe_leap.DEFAULT_VID)
    parser.add_argument("--pid", type=lambda text: int(text, 0), default=probe_leap.DEFAULT_PID)
    parser.add_argument("--unique-id", help="AVFoundation unique id, if not matched automatically")
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=240, help="240 matches the recorded sequence")
    parser.add_argument("--leapcap", type=Path, default=ROOT / "build" / "leapcap")
    parser.add_argument("--out", type=Path, default=ROOT / "probe_out")
    return parser.parse_args()


def open_leap(usb, args, attempts=1):
    error = None
    for attempt in range(attempts):
        if attempt:
            time.sleep(REOPEN_DELAY_S)
        device, error = probe_leap.open_quietly(usb, args)
        if device is not None:
            return device
    sys.exit(f"Could not open the Leap: {error}")


def find_capture_device(args):
    check, found = probe_leap.check_device_visible(probe_leap.run_leapcap_list(args.leapcap), args)
    if check["status"] != probe_leap.PASS:
        sys.exit(f"Capture device not available: {check['detail']}")
    return found["unique_id"]


def replay_all(usb, args, rows):
    device = open_leap(usb, args)
    try:
        return {"replay": bringup.replay(device, rows)}
    finally:
        device.close()


def replay_with_reset(usb, args, rows):
    """Follow the recorded order: replay, reset, reopen, then replay the rest."""
    device = open_leap(usb, args)
    results = {"before_reset": bringup.replay(device, rows[: bringup.RESET_SPLIT])}
    try:
        device.reset()
        results["reset"] = "ok"
    except uvc_control.LibusbError as error:
        results["reset"] = str(error)
    device.close()

    # Skip the row at the split, which is the marker OpenMotion also skips.
    device = open_leap(usb, args, attempts=REOPEN_ATTEMPTS)
    try:
        results["after_reset"] = bringup.replay(device, rows[bringup.RESET_SPLIT + 1 :])
    finally:
        device.close()
    return results


def read_state(usb, args):
    device, error = probe_leap.open_quietly(usb, args)
    if device is None:
        return {"open_error": error}
    with device:
        return device.read_stream_state()


def main():
    args = parse_args()
    if not args.leapcap.exists():
        sys.exit(f"{args.leapcap} is missing. Run `make leapcap` first.")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    directory = args.out / f"bringup-{args.scenario}-{stamp}"
    frames_directory = directory / "frames"
    samples_directory = directory / "samples"
    samples_directory.mkdir(parents=True)

    rows = bringup.load_sequence()
    report = {"scenario": args.scenario, "rows": len(rows), "width": args.width, "height": args.height}
    unique_id = find_capture_device(args)

    with uvc_control.Libusb() as usb:
        report["state_before"] = read_state(usb, args)

        if args.scenario == "before":
            report["writes"] = replay_all(usb, args, rows)
        elif args.scenario == "reset":
            report["writes"] = replay_with_reset(usb, args, rows)
            unique_id = find_capture_device(args)

        if args.scenario != "during":
            time.sleep(SETTLE_S)

        run = probe_leap.CaptureRun(args.leapcap, unique_id, args.width, args.height, "yuvs", frames_directory)
        run.start()

        if args.scenario == "during":
            time.sleep(CAPTURE_LEAD_S)
            report["writes"] = replay_all(usb, args, rows)

        arrived = run.wait_for_frame(timeout=FRAME_TIMEOUT_S)
        report["state_during"] = read_state(usb, args)
        if arrived:
            report["sample"] = probe_leap.sample(run, samples_directory, "first", keep_images=True)
        run.finish()

    report["frames_arrived"] = arrived is not None
    report["helper_events"] = [event for event in run.events if event.get("event") != "frame"]
    report["saved_frame_events"] = len(run.frames)
    (directory / "report.json").write_text(json.dumps(report, indent=2, default=str))
    shutil.rmtree(frames_directory, ignore_errors=True)

    print()
    print("FRAMES ARRIVED" if arrived else "No frames arrived")
    print(f"Scenario: {args.scenario}, {args.width}x{args.height}")
    for name, value in report["writes"].items():
        if isinstance(value, dict):
            print(f"  {name}: {value['attempted']} writes, {value['failed']} failed")
        else:
            print(f"  {name}: {value}")
    print(f"Report: {directory / 'report.json'}")


if __name__ == "__main__":
    main()

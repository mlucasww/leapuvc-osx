#!/usr/bin/env python3
"""Phase 0 hardware probe for an unlocked Leap Motion Controller on macOS.

This answers four questions, then recommends a capture path:

  1. Does AVFoundation list the Leap with a usable format?
  2. Do both camera images survive in the bytes it delivers?
  3. Do UVC controls work without root while it streams?
  4. Can the factory calibration be read and trusted?

Run it from Terminal.app, from the macos/ folder:

    make probe

Results go to probe_out/<timestamp>/ as report.md and report.json, with sample
frames alongside. The probe changes exposure, gain and LEDs while it runs, and
restores exposure and gain afterwards.
"""

import argparse
import json
import platform
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from leapmac import calibration_blob, frames, uvc_control  # noqa: E402
from leapmac.probe_decision import BLOCKED, FAIL, INCONCLUSIVE, PASS, decide  # noqa: E402

# The commonly reported Leap IDs, not confirmed by the manual. Override them
# with --vid and --pid if the probe does not find the device.
DEFAULT_VID = 0xF182
DEFAULT_PID = 0x0003

# Long enough for slow first-time imports and USB opens, as well as every step.
CAPTURE_SECONDS = 30
SAVE_EVERY = 5
FIRST_FRAME_TIMEOUT_S = 10
SETTLE_S = 0.6
SKIPPED = "skipped"

# Thresholds for check 2. Below this contrast the scene cannot prove anything.
MIN_SCENE_STD = 4.0
MIN_STEREO_CORRELATION = 0.3
MIN_BRIGHTNESS_RATIO = 1.3


def result(status, detail, **evidence):
    return {"status": status, "detail": detail, "evidence": evidence}


class CaptureRun:
    """Runs `leapcap capture` and tracks the JSON events it prints."""

    def __init__(self, leapcap, unique_id, width, height, pixel_format, directory):
        self.directory = directory
        self.command = [
            str(leapcap), "capture",
            "--unique-id", unique_id,
            "--out", str(directory),
            "--width", str(width),
            "--height", str(height),
            "--pixel-format", pixel_format,
            "--seconds", str(CAPTURE_SECONDS),
            "--every", str(SAVE_EVERY),
        ]
        self.events = []
        self.frames = []
        self.ended = False
        self.condition = threading.Condition()

    def start(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.stderr = open(self.directory.parent / "leapcap.stderr.log", "a")
        self.process = subprocess.Popen(
            self.command, stdout=subprocess.PIPE, stderr=self.stderr, text=True
        )
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        for line in self.process.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            with self.condition:
                self.events.append(event)
                if event.get("event") == "frame":
                    self.frames.append(event)
                self.condition.notify_all()
        with self.condition:
            self.ended = True
            self.condition.notify_all()

    def latest_index(self):
        with self.condition:
            return self.frames[-1]["index"] if self.frames else 0

    def wait_for_frame(self, timeout, after_index=0):
        """Return a frame event newer than after_index, or None."""
        deadline = time.monotonic() + timeout
        with self.condition:
            while True:
                if self.frames and self.frames[-1]["index"] > after_index:
                    return self.frames[-1]
                remaining = deadline - time.monotonic()
                if self.ended or remaining <= 0:
                    return None
                self.condition.wait(remaining)

    def event(self, kind):
        with self.condition:
            return next((event for event in self.events if event.get("event") == kind), None)

    def finish(self):
        """Let the helper finish, so its dropped-frame summary is reported."""
        try:
            self.process.wait(timeout=CAPTURE_SECONDS + 10)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=10)
        self.reader.join(timeout=5)
        self.stderr.close()


def open_quietly(usb, args):
    """Open the Leap. Returns (device, error text), because failing is a result here."""
    try:
        device = usb.open(args.vid, args.pid)
    except (uvc_control.LibusbError, ValueError) as error:
        return None, str(error)
    if device is None:
        return None, f"no USB device {args.vid:04x}:{args.pid:04x}"
    return device, None


def probe_usb(usb, args):
    """Check the device is on USB and record what its descriptors say."""
    device, error = open_quietly(usb, args)
    if device is None:
        return None, {
            "libusb": usb.path,
            "found": False,
            "open_error": error,
            "all_devices": [asdict(info) for info in usb.list_devices()],
        }
    with device:
        return asdict(device.topology), {
            "libusb": usb.path,
            "found": True,
            "device": asdict(device.info),
            "topology": asdict(device.topology),
        }


def run_leapcap_list(leapcap):
    completed = subprocess.run(
        [str(leapcap), "list"], capture_output=True, text=True, timeout=90
    )
    for line in completed.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("event") == "devices":
            return event
    return None


def matches_leap(device, vendor_id, product_id):
    unique_id = device.get("unique_id", "").lower()
    model_id = device.get("model_id", "")
    return (
        unique_id.endswith(f"{vendor_id:04x}{product_id:04x}")
        or (f"VendorID_{vendor_id}" in model_id and f"ProductID_{product_id}" in model_id)
        or "leap" in device.get("name", "").lower()
    )


def check_device_visible(listing, args):
    """Check 1: find the Leap in AVFoundation's device list."""
    if listing is None:
        return result(BLOCKED, "leapcap list produced no device report"), None
    authorization = listing.get("authorization")
    if authorization != "authorized":
        return (
            result(
                BLOCKED,
                f"Camera access is {authorization}. Grant it to the app running this probe in "
                "System Settings > Privacy & Security > Camera, or run it from Terminal.app.",
            ),
            None,
        )

    devices = listing.get("devices", [])
    names = [device.get("name") for device in devices]
    if args.unique_id:
        found = next((d for d in devices if d.get("unique_id") == args.unique_id), None)
    else:
        found = next((d for d in devices if matches_leap(d, args.vid, args.pid)), None)
    if found is None:
        return result(FAIL, "AVFoundation does not list the Leap.", devices=names), None

    sized = [
        form for form in found.get("formats", [])
        if form["width"] == args.width and form["height"] == args.height
    ]
    if not sized:
        return result(FAIL, f"The Leap has no {args.width}x{args.height} format.", device=found), found

    subtypes = sorted({form["subtype"] for form in sized})
    return result(PASS, f"Listed as {found.get('name')!r}, subtypes {subtypes}.", device=found), found


def read_calibration(device, directory):
    """Check 4: read the blob twice, then compare and sanity-check it."""
    first, high_bytes = device.read_calibration_bytes()
    second, _ = device.read_calibration_bytes()
    (directory / "calibration.bin").write_bytes(first)

    parsed = calibration_blob.parse_blob(first)
    problems = calibration_blob.plausibility_problems(parsed)
    evidence = {"parsed": parsed, "high_bytes_seen": high_bytes, "problems": problems}
    if first != second:
        differing = sum(a != b for a, b in zip(first, second))
        return result(FAIL, f"Two reads differ in {differing} of 156 bytes.", **evidence)
    if problems:
        return result(INCONCLUSIVE, "Reads match, but some values look unusual.", **evidence)
    return result(PASS, f"Reads match. Baseline value is {parsed['baseline']}.", **evidence)


def attempt_calibration(device, directory):
    try:
        return read_calibration(device, directory)
    except uvc_control.LibusbError as error:
        return result(FAIL, f"USB request failed: {error}")
    except ValueError as error:
        return result(FAIL, str(error))


def sample(run, samples_directory, label, keep_images):
    """Wait for the settings to land, then decode a fresh frame."""
    time.sleep(SETTLE_S)
    event = run.wait_for_frame(timeout=3.0, after_index=run.latest_index())
    if event is None:
        return {"label": label, "error": "no fresh frame arrived"}

    path = Path(event["path"])
    raw = path.read_bytes()
    info = {
        "label": label,
        "index": event["index"],
        "width": event["width"],
        "height": event["height"],
        "pixel_format": event["pixel_format"],
        "bytes_per_row": event["bytes_per_row"],
    }
    try:
        interleaved = frames.to_interleaved(
            raw, event["width"], event["height"], event["pixel_format"]
        )
    except ValueError as error:
        info["error"] = str(error)
        return info

    left, right = frames.deinterleave(interleaved)
    info["meta"] = asdict(frames.parse_embedded(interleaved))
    info["stats"] = frames.frame_stats(left, right)

    shutil.copy(path, samples_directory / f"{label}.raw")
    if keep_images:
        import cv2

        cv2.imwrite(str(samples_directory / f"{label}_left.png"), left.copy())
        cv2.imwrite(str(samples_directory / f"{label}_right.png"), right.copy())
    return info


def run_control_sequence(device, run, samples_directory, keep_images):
    """Change settings during streaming and record what each frame reports."""
    steps = {}
    errors = {}

    def step(label, action=None):
        started = time.monotonic()
        if action is not None:
            try:
                action()
            except (uvc_control.LibusbError, ValueError) as error:
                errors[label] = str(error)
        action_seconds = time.monotonic() - started
        steps[label] = sample(run, samples_directory, label, keep_images)
        steps[label]["action_seconds"] = round(action_seconds, 3)

    original = {}
    for name in ("exposure", "gain"):
        try:
            original[name] = device.get(name)
        except (uvc_control.LibusbError, ValueError) as error:
            errors[f"read_{name}"] = str(error)

    # Exposure stays high enough for the scene to show through the gain tests.
    step("baseline", lambda: device.set_leds(True))
    step("exposure_2000", lambda: device.set("exposure", 2000))
    step("exposure_8000", lambda: device.set("exposure", 8000))
    step("gain_plain_20", lambda: device.set("gain", 20))
    step("gain_plain_40", lambda: device.set("gain", 40))
    step("gain_selector_20", lambda: device.set("gain", uvc_control.GAIN_SELECTOR | 20))
    step("gain_selector_40", lambda: device.set("gain", uvc_control.GAIN_SELECTOR | 40))
    step("leds_off", lambda: device.set_leds(False))
    step("leds_on", lambda: device.set_leds(True))

    for name, value in original.items():
        try:
            device.set(name, value)
        except (uvc_control.LibusbError, ValueError) as error:
            errors[f"restore_{name}"] = str(error)

    return steps, errors, original


def _meta(steps, label, field):
    return steps.get(label, {}).get("meta", {}).get(field)


def _mean(steps, label):
    return steps.get(label, {}).get("stats", {}).get("left_mean")


def _ratio(high, low):
    if high is None or low is None or low <= 0:
        return None
    return high / low


def evaluate_controls(steps, errors):
    """Work out which controls changed the frames they should have changed."""
    gain_plain = _meta(steps, "gain_plain_20", "gain") == 20 and _meta(steps, "gain_plain_40", "gain") == 8
    gain_selector = (
        _meta(steps, "gain_selector_20", "gain") == 20 and _meta(steps, "gain_selector_40", "gain") == 8
    )
    low, high = _meta(steps, "exposure_2000", "exposure"), _meta(steps, "exposure_8000", "exposure")
    exposure_changed = low is not None and high is not None and low != high
    exposure_matches = low == 2000 % 512 and high == 8000 % 512
    brightness_ratio = _ratio(_mean(steps, "exposure_8000"), _mean(steps, "exposure_2000"))
    led_ratio = _ratio(_mean(steps, "leds_on"), _mean(steps, "leds_off"))
    return {
        "gain_plain_form_works": gain_plain,
        "gain_selector_form_works": gain_selector,
        "exposure_metadata_changed": exposure_changed,
        "exposure_metadata_matches_microseconds": exposure_matches,
        "brightness_ratio_8000_over_2000": brightness_ratio,
        "led_on_over_off_brightness_ratio": led_ratio,
        "request_errors": errors,
    }


def check_controls(findings):
    """Check 3: did any control take effect during streaming?"""
    ratio = findings["brightness_ratio_8000_over_2000"]
    responded = (
        findings["gain_plain_form_works"]
        or findings["gain_selector_form_works"]
        or findings["exposure_metadata_changed"]
        or (ratio is not None and ratio >= MIN_BRIGHTNESS_RATIO)
    )
    if responded:
        return result(PASS, "Controls changed the stream without root.", **findings)
    if findings["request_errors"]:
        return result(FAIL, "Control requests failed.", **findings)
    return result(FAIL, "Requests were accepted, but the stream did not change.", **findings)


def check_bytes(steps, requested_size, findings):
    """Check 2: do the delivered bytes hold both raw camera images?"""
    baseline = steps.get("exposure_8000") or steps.get("baseline", {})
    stats = baseline.get("stats")
    if stats is None:
        return result(FAIL, f"Frames could not be decoded: {baseline.get('error', 'no sample')}", samples=steps)

    # A video-range conversion would clamp every value into 16 to 235.
    all_stats = [step["stats"] for step in steps.values() if "stats" in step]
    full_range_seen = any(
        entry["below_16_fraction"] > 0 or entry["above_235_fraction"] > 0 for entry in all_stats
    )
    size = (baseline["width"], baseline["height"])
    evidence = {
        "stats": stats,
        "pixel_format": baseline["pixel_format"],
        "bytes_per_row": baseline["bytes_per_row"],
        "full_range_seen": full_range_seen,
    }
    if size != requested_size:
        return result(FAIL, f"Delivered {size}, not {requested_size}: macOS is scaling.", **evidence)
    if stats["mean_abs_left_right"] < 0.5:
        return result(FAIL, "Left and right images are identical.", **evidence)
    if min(stats["left_std"], stats["right_std"]) < MIN_SCENE_STD:
        return result(INCONCLUSIVE, "The scene is too flat to judge. Aim the Leap at something detailed.", **evidence)

    # Exact values landing in the last 12 bytes show those bytes arrive unaltered.
    metadata_ok = (
        findings["gain_plain_form_works"]
        or findings["gain_selector_form_works"]
        or findings["exposure_metadata_matches_microseconds"]
    )
    stereo_ok = stats["best_correlation"] >= MIN_STEREO_CORRELATION
    if not stereo_ok:
        return result(FAIL, "The second image does not match the first as a stereo pair.", **evidence)
    if not metadata_ok:
        return result(INCONCLUSIVE, "The images match as a stereo pair, but metadata did not follow the controls.", **evidence)
    if not full_range_seen:
        return result(INCONCLUSIVE, "No values outside 16 to 235, so macOS may be converting the range.", **evidence)
    return result(PASS, "Metadata follows the controls, and the images match as a stereo pair.", **evidence)


def start_capture(args, unique_id, directory):
    """Start streaming, trying yuvs and then 2vuy.

    Returns (run, pixel_format, failed_attempts). A failed attempt runs to its
    end, so the helper still reports its dropped-frame counts and warnings.
    """
    run = None
    attempts = []
    for pixel_format in ("yuvs", "2vuy"):
        run = CaptureRun(args.leapcap, unique_id, args.width, args.height, pixel_format, directory)
        run.start()
        if run.wait_for_frame(timeout=FIRST_FRAME_TIMEOUT_S):
            return run, pixel_format, attempts
        run.finish()
        attempts.append({"pixel_format": pixel_format, "events": run.events})
    return run, None, attempts


def check_failed_capture(attempts):
    """Judge a capture with no frames. Only a reported error counts as a failure."""
    errors = [
        event
        for attempt in attempts
        for event in attempt["events"]
        if event.get("event") == "error"
    ]
    if any("authorization" in error for error in errors):
        return result(BLOCKED, f"Could not stream: {errors[-1]['message']}", attempts=attempts)
    if len(errors) == len(attempts):
        return result(FAIL, f"Could not stream: {errors[-1]['message']}", attempts=attempts)
    return result(
        INCONCLUSIVE,
        "The session started but delivered no frames. See the helper events in report.json.",
        attempts=attempts,
    )


def write_report(directory, report):
    (directory / "report.json").write_text(json.dumps(report, indent=2, default=str))

    lines = [
        "# Leap probe report",
        "",
        f"Recorded {report['recorded_at']} on macOS {report['environment']['macos']} "
        f"({report['environment']['machine']}).",
        "",
        f"**{report['decision']['summary']}**",
        "",
        "| Check | Status | Detail |",
        "|---|---|---|",
    ]
    for name, check in report["checks"].items():
        lines.append(f"| {name} | {check['status']} | {check['detail']} |")

    device = report["usb"].get("device")
    if device:
        lines += [
            "",
            "## USB device",
            "",
            f"- {device['manufacturer']} {device['product']} "
            f"(`{device['vendor_id']:04x}:{device['product_id']:04x}`), {device['speed']} speed",
            f"- Serial: {device['serial'] or 'none reported'}",
        ]
        for form in report["usb"]["topology"]["formats"]:
            lines += ["", f"### Format {form['fourcc']}, {form['bits_per_pixel']} bits per pixel", ""]
            for frame in form["frames"]:
                rates = ", ".join(
                    f"{10_000_000 / interval:.1f}" for interval in frame["intervals_100ns"] if interval
                )
                lines.append(f"- {frame['width']}x{frame['height']}: {rates} fps")

    capture = report.get("capture") or {}
    if capture.get("done"):
        lines += ["", "## Capture", "", f"```json\n{json.dumps(capture['done'], indent=2)}\n```"]

    (directory / "report.md").write_text("\n".join(lines) + "\n")


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--vid", type=lambda text: int(text, 0), default=DEFAULT_VID)
    parser.add_argument("--pid", type=lambda text: int(text, 0), default=DEFAULT_PID)
    parser.add_argument("--unique-id", help="AVFoundation unique id, if the Leap is not matched automatically")
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--leapcap", type=Path, default=ROOT / "build" / "leapcap")
    parser.add_argument("--out", type=Path, default=ROOT / "probe_out")
    parser.add_argument("--keep-frames", action="store_true", help="keep every captured frame")
    return parser.parse_args()


def run_probe(args, directory, usb):
    skipped = result(SKIPPED, "Not reached.")
    checks = {
        "1. device visible": dict(skipped),
        "2. bytes intact": dict(skipped),
        "3. controls work": dict(skipped),
        "4. calibration valid": dict(skipped),
    }
    report = {"checks": checks, "usb": {}, "capture": None}

    topology, report["usb"] = probe_usb(usb, args)
    if topology is None:
        checks["1. device visible"] = result(
            BLOCKED,
            f"Could not open the Leap ({report['usb']['open_error']}). Check the cable, or pass "
            "--vid and --pid using the list in report.json.",
        )
        return report
    if topology["vc_interface"] is None:
        checks["1. device visible"] = result(
            FAIL, "The device has no UVC interface. Confirm it has been unlocked on Windows."
        )
        return report

    listing = run_leapcap_list(args.leapcap)
    checks["1. device visible"], found = check_device_visible(listing, args)
    if found is None or checks["1. device visible"]["status"] != PASS:
        return report

    # Read calibration while idle, and hold no USB handle when streaming starts.
    device, error = open_quietly(usb, args)
    if device is None:
        checks["4. calibration valid"] = result(FAIL, f"Could not open the Leap: {error}")
    else:
        checks["4. calibration valid"] = attempt_calibration(device, directory)
        device.close()

    # Import before streaming, because the first import can take many seconds.
    try:
        import cv2  # noqa: F401

        keep_images = True
    except ImportError:
        keep_images = False

    frames_directory = directory / "frames"
    samples_directory = directory / "samples"
    samples_directory.mkdir()
    capture_started = time.monotonic()
    run, pixel_format, attempts = start_capture(args, found["unique_id"], frames_directory)
    report["capture"] = {"pixel_format": pixel_format, "started": run.event("started")}

    if pixel_format is None:
        checks["1. device visible"] = check_failed_capture(attempts)
        return report

    timing = {"first_frame_seconds": round(time.monotonic() - capture_started, 3)}
    report["capture"]["timing"] = timing
    device, error = open_quietly(usb, args)
    timing["usb_open_seconds"] = round(time.monotonic() - capture_started, 3)
    if device is None:
        # Frames can still be judged even though controls cannot be reached.
        steps = {"baseline": sample(run, samples_directory, "baseline", keep_images)}
        errors, original = {"open_during_streaming": error}, {}
    else:
        steps, errors, original = run_control_sequence(device, run, samples_directory, keep_images)
    timing["controls_done_seconds"] = round(time.monotonic() - capture_started, 3)
    findings = evaluate_controls(steps, errors)
    checks["2. bytes intact"] = check_bytes(steps, (args.width, args.height), findings)
    checks["3. controls work"] = check_controls(findings)
    report["capture"]["steps"] = steps
    report["capture"]["original_controls"] = original

    if device is not None:
        if checks["4. calibration valid"]["status"] == FAIL:
            retry = attempt_calibration(device, directory)
            retry["detail"] = f"Read during streaming, after the idle read failed. {retry['detail']}"
            checks["4. calibration valid"] = retry
        device.close()

    run.finish()
    report["capture"]["done"] = run.event("done")
    report["capture"]["warnings"] = [
        event for event in run.events if event.get("event") == "warning"
    ]
    if not args.keep_frames:
        shutil.rmtree(frames_directory, ignore_errors=True)
    return report


def main():
    args = parse_args()
    if not args.leapcap.exists():
        sys.exit(f"{args.leapcap} is missing. Run `make leapcap` first.")

    directory = args.out / datetime.now().strftime("%Y%m%d-%H%M%S")
    directory.mkdir(parents=True)

    with uvc_control.Libusb() as usb:
        report = run_probe(args, directory, usb)

    report["recorded_at"] = datetime.now().isoformat(timespec="seconds")
    report["environment"] = {
        "macos": platform.mac_ver()[0],
        "machine": platform.machine(),
        "python": platform.python_version(),
    }
    checks = report["checks"]
    report["decision"] = decide(*(checks[name]["status"] for name in checks))
    write_report(directory, report)

    print()
    for name, check in checks.items():
        print(f"{check['status']:>13}  {name}: {check['detail']}")
    print()
    print(report["decision"]["summary"])
    print(f"Report: {directory / 'report.md'}")


if __name__ == "__main__":
    main()

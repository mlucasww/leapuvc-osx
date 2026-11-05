#!/usr/bin/env python3
"""Measure what each camera control does, without any video windows.

Commands, run from Terminal.app in the macos/ folder:

    .venv/bin/python tools/control_diag.py state     read controls, change nothing
    .venv/bin/python tools/control_diag.py trackbar  check if a slider fires when created
    .venv/bin/python tools/control_diag.py sweep     step through controls and test rectify

`sweep` uses the documented controls only, through the same methods as the
examples, then restores the exposure, gain and gamma values it found.
"""

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from leapmac import capture, uvc_control  # noqa: E402
from leapmac.devices import DEFAULT_PID, DEFAULT_VID  # noqa: E402

CONTROL_NAMES = ("exposure", "gain", "digital_gain", "gamma", "contrast", "dark_frame_interval")
SETTLE_S = 0.4
FRAME_TIMEOUT_S = 2.0


def read_state(device):
    """Read each control's current value and reported range."""
    state = {}
    for name in CONTROL_NAMES:
        entry = {}
        try:
            entry["current"] = device.get(name)
        except (uvc_control.LibusbError, ValueError) as error:
            entry["error"] = str(error)
        try:
            entry["min"], entry["max"], entry["default"] = device.get_range(name)
        except (uvc_control.LibusbError, ValueError) as error:
            entry["range_error"] = str(error)
        state[name] = entry
    return state


def save(args, kind, report):
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"{kind}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps(report, indent=2, default=str))
    print(f"Report: {path}")


def command_state(args):
    with uvc_control.Libusb() as usb:
        device = usb.open(DEFAULT_VID, DEFAULT_PID)
        if device is None:
            sys.exit("Leap not found")
        with device:
            save(args, "control-state", read_state(device))


def command_trackbar(args):
    """Check whether OpenCV calls a slider's callback when it's created."""
    calls = []
    cv2.namedWindow("Trackbar check")
    cv2.createTrackbar("Value", "Trackbar check", 7, 16, calls.append)
    # This matches the example's Rectify slider, which it reads on every frame.
    cv2.createTrackbar("Rectify", "Trackbar check", 0, 1, lambda value: 0)
    calls_after_create = list(calls)
    for _ in range(30):
        cv2.waitKey(10)
    positions = {
        "Value, created at 7": cv2.getTrackbarPos("Value", "Trackbar check"),
        "Rectify, created at 0": cv2.getTrackbarPos("Rectify", "Trackbar check"),
    }
    cv2.destroyAllWindows()
    save(
        args,
        "trackbar",
        {
            "opencv": cv2.__version__,
            "calls_after_create": calls_after_create,
            "calls_after_event_loop": calls,
            "positions": positions,
        },
    )


class Sweep:
    def __init__(self, leap, directory):
        self.leap = leap
        self.directory = directory
        self.steps = []

    def latest_image(self):
        self.leap.read()
        deadline = time.monotonic() + FRAME_TIMEOUT_S
        while time.monotonic() < deadline:
            fresh, image = self.leap.read()
            if fresh:
                return image.copy()
            time.sleep(0.002)
        return None

    def step(self, label, action=None):
        if action is not None:
            action()
        time.sleep(SETTLE_S)
        image = self.latest_image()
        if image is None:
            self.steps.append({"label": label, "error": "no frame"})
            return None
        cv2.imwrite(str(self.directory / f"{label}.png"), image[0])
        self.steps.append(
            {
                "label": label,
                "embedded": self.leap.embeddedLine,
                "left_mean": round(float(image[0].mean()), 2),
                "right_mean": round(float(image[1].mean()), 2),
                "left_max": int(image[0].max()),
                "left_std": round(float(image[0].std()), 2),
            }
        )
        return image


def describe_rectification(leap, image, directory):
    results = {}
    for index, camera in enumerate(leap.cameras):
        maps = leap.calibration[camera]["undistortMaps"]
        rectified = cv2.remap(image[index], maps[0], maps[1], cv2.INTER_LINEAR)
        cv2.imwrite(str(directory / f"rectified_{camera}.png"), rectified)
        results[camera] = {
            "raw_mean": round(float(image[index].mean()), 2),
            "rectified_mean": round(float(rectified.mean()), 2),
            "rectified_nonzero_fraction": round(float((rectified > 0).mean()), 4),
            "map_x_range": [int(maps[0][..., 0].min()), int(maps[0][..., 0].max())],
            "map_y_range": [int(maps[0][..., 1].min()), int(maps[0][..., 1].max())],
            "center_maps_to": [int(value) for value in maps[0][240, 320]],
            "camera_matrix": leap.calibration[camera]["extrinsics"]["cameraMatrix"].tolist(),
            "dist_coeffs": leap.calibration[camera]["intrinsics"]["distCoeffs"].tolist(),
            "radial": leap.calibration[camera]["intrinsics"]["radial"],
        }
    return results


def command_sweep(args):
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    directory = args.out / f"sweep-{stamp}"
    directory.mkdir(parents=True)

    leap = capture.leapImageThread(resolution=(640, 480), timeout=30)
    leap.start()
    state = leap.controls.call(read_state)

    sweep = Sweep(leap, directory)
    sweep.step("baseline")

    for value in (0, 1, 4, 8, 16):
        sweep.step(f"digital_gain_{value}", lambda value=value: leap.setDigitalGain(value))
    sweep.step("digital_gain_restored", lambda: leap.setDigitalGain(state["digital_gain"].get("current", 4)))

    for value in (0, 16, 32, 63):
        sweep.step(f"gain_{value}", lambda value=value: leap.setGain(value))
    sweep.step("gain_restored", lambda: leap.setGain(state["gain"].get("current", 16)))

    sweep.step("gamma_off", lambda: leap.setGammaEnabled(False))
    sweep.step("gamma_on", lambda: leap.setGammaEnabled(True))
    sweep.step("gamma_restored", lambda: leap.set(cv2.CAP_PROP_GAMMA, state["gamma"].get("current", 1)))

    sweep.step("hdr_on", lambda: leap.setHDR(True))
    sweep.step("hdr_off", lambda: leap.setHDR(False))

    for value in (1000, 4000, 16000, 32222):
        sweep.step(f"exposure_{value}", lambda value=value: leap.setExposure(value))
    sweep.step("exposure_restored", lambda: leap.setExposure(state["exposure"].get("current", 300)))

    image = sweep.step("final")
    rectification = describe_rectification(leap, image, directory) if image is not None else None
    state_after = leap.controls.call(read_state)

    leap.stop()
    leap.join(timeout=10)

    save(
        args,
        "sweep",
        {
            "images": str(directory),
            "state_before": state,
            "state_after": state_after,
            "steps": sweep.steps,
            "rectification": rectification,
        },
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("state", "trackbar", "sweep"))
    parser.add_argument("--out", type=Path, default=ROOT / "probe_out")
    args = parser.parse_args()
    {"state": command_state, "trackbar": command_trackbar, "sweep": command_sweep}[args.command](args)


if __name__ == "__main__":
    main()

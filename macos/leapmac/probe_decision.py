"""Turn the four Phase 0 check results into a capture path.

Each check ends as one of:
  pass          the path works
  fail          the path does not work, so use the fallback
  inconclusive  a human needs to look at the saved evidence
  blocked       something outside the test stopped it, such as camera permission

Only `fail` selects a fallback. A permission problem must never push the
project onto the root-only path.
"""

PASS = "pass"
FAIL = "fail"
INCONCLUSIVE = "inconclusive"
BLOCKED = "blocked"


def decide(device_visible, bytes_intact, controls_work, calibration_valid):
    """Return the recommended path, given each check's status string."""
    checks = {
        "1. device visible": device_visible,
        "2. bytes intact": bytes_intact,
        "3. controls work": controls_work,
        "4. calibration valid": calibration_valid,
    }

    blocked = [name for name, status in checks.items() if status == BLOCKED]
    if blocked:
        return {
            "path": None,
            "summary": "Blocked. Fix the reported problem and run the probe again.",
            "open_checks": blocked,
        }

    if device_visible == FAIL or bytes_intact == FAIL:
        frames, controls, path = "libuvc as root", "libusb as root", "C"
    else:
        frames = "AVFoundation helper"
        if controls_work == FAIL:
            controls, path = "IOKit seize in the helper", "B"
        else:
            controls, path = "libusb on endpoint 0", "A"

    calibration = "checkerboard" if calibration_valid == FAIL else "device blob"
    undecided = [name for name, status in checks.items() if status == INCONCLUSIVE]

    summary = f"Path {path}: frames via {frames}, controls via {controls}, calibration from {calibration}."
    if undecided:
        summary += " Provisional until the open checks are reviewed."

    return {
        "path": path,
        "frames": frames,
        "controls": controls,
        "calibration": calibration,
        "summary": summary,
        "open_checks": undecided,
    }

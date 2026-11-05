"""Find the Leap among the cameras AVFoundation reports."""

import json
import subprocess

# The Leap's USB IDs, confirmed on real hardware in Phase 0.
DEFAULT_VID = 0xF182
DEFAULT_PID = 0x0003


def run_leapcap_list(leapcap):
    completed = subprocess.run([str(leapcap), "list"], capture_output=True, text=True, timeout=90)
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

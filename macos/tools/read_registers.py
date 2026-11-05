#!/usr/bin/env python3
"""Read the Leap's registers, and optionally compare calibration with a backup.

This selects only the address ranges that Leap's own software reads: 0x00 to
0x0D and 0x64 to 0xFF. It deliberately avoids 0x62 and 0x63, which leapd selects
without reading and which may act as commands, and it writes no register data.

    .venv/bin/python tools/read_registers.py --compare probe_out/<run>/calibration.bin
"""

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from leapmac import uvc_control  # noqa: E402

DEFAULT_VID = 0xF182
DEFAULT_PID = 0x0003
LOW_ADDRESSES = range(0x00, 0x0E)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--vid", type=lambda text: int(text, 0), default=DEFAULT_VID)
    parser.add_argument("--pid", type=lambda text: int(text, 0), default=DEFAULT_PID)
    parser.add_argument("--compare", type=Path, help="a saved calibration.bin to compare against")
    parser.add_argument("--out", type=Path, default=ROOT / "probe_out")
    return parser.parse_args()


def describe_difference(current, backup):
    """Return the differing offsets between two calibration blobs."""
    if len(current) != len(backup):
        return {"sizes_differ": [len(current), len(backup)]}
    return {
        "offsets": [offset for offset, (a, b) in enumerate(zip(current, backup)) if a != b],
    }


def main():
    args = parse_args()

    with uvc_control.Libusb() as usb:
        device = usb.open(args.vid, args.pid)
        if device is None:
            sys.exit(f"No USB device {args.vid:04x}:{args.pid:04x}")
        with device:
            low_first = device.read_registers(LOW_ADDRESSES)
            low_second = device.read_registers(LOW_ADDRESSES)
            calibration, high_bytes = device.read_calibration_bytes()

    report = {
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
        "low_addresses_first": [f"{value:04x}" for value in low_first],
        "low_addresses_second": [f"{value:04x}" for value in low_second],
        "low_addresses_stable": low_first == low_second,
        "calibration_sha256": hashlib.sha256(calibration).hexdigest(),
        "calibration_high_bytes_seen": high_bytes,
    }
    if args.compare:
        backup = args.compare.read_bytes()
        report["calibration_matches_backup"] = calibration == backup
        report["backup_sha256"] = hashlib.sha256(backup).hexdigest()
        report["difference"] = describe_difference(calibration, backup)

    args.out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = args.out / f"registers-{stamp}.json"
    (args.out / f"registers-{stamp}.bin").write_bytes(calibration)
    path.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    print(f"Report: {path}")


if __name__ == "__main__":
    main()

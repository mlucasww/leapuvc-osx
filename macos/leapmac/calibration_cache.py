"""Cache each Leap's factory calibration, keyed by its serial number.

Reading calibration takes about a second of USB traffic, and it must happen
before streaming starts. It never changes, so it only needs reading once.
A blob that looks wrong is returned but never cached.
"""

from pathlib import Path

from . import calibration_blob

DEFAULT_DIRECTORY = Path.home() / "Library" / "Caches" / "leapmac"


def cache_path(serial, directory=DEFAULT_DIRECTORY):
    if not serial or not serial.isalnum():
        return None
    return Path(directory) / f"{serial}.bin"


def is_valid(blob):
    if len(blob) != calibration_blob.BLOB_SIZE:
        return False
    return not calibration_blob.plausibility_problems(calibration_blob.parse_blob(blob))


def load_calibration(device, directory=DEFAULT_DIRECTORY):
    """Return (blob, source), where source is "cache" or "device"."""
    path = cache_path(device.read_serial(), directory)

    if path is not None and path.exists():
        cached = path.read_bytes()
        if is_valid(cached):
            return cached, "cache"

    blob, _ = device.read_calibration_bytes()
    if path is not None and is_valid(blob):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)
    return blob, "device"

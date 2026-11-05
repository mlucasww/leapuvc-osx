from leapmac import calibration_cache
from test_calibration_blob import make_blob


class FakeDevice:
    def __init__(self, serial, blob):
        self.serial = serial
        self.blob = blob
        self.reads = 0

    def read_serial(self):
        return self.serial

    def read_calibration_bytes(self):
        self.reads += 1
        return self.blob, 0


def test_first_read_comes_from_the_device_and_is_cached(tmp_path):
    device = FakeDevice("LP123", make_blob())

    blob, source = calibration_cache.load_calibration(device, tmp_path)

    assert source == "device"
    assert blob == device.blob
    assert (tmp_path / "LP123.bin").read_bytes() == device.blob


def test_second_read_uses_the_cache(tmp_path):
    device = FakeDevice("LP123", make_blob())
    calibration_cache.load_calibration(device, tmp_path)

    blob, source = calibration_cache.load_calibration(device, tmp_path)

    assert source == "cache"
    assert blob == device.blob
    assert device.reads == 1


def test_two_devices_do_not_share_a_cache(tmp_path):
    first = FakeDevice("LP1", make_blob(left_focal=130.0))
    second = FakeDevice("LP2", make_blob(left_focal=140.0))
    calibration_cache.load_calibration(first, tmp_path)

    blob, source = calibration_cache.load_calibration(second, tmp_path)

    assert source == "device"
    assert blob == second.blob


def test_implausible_blob_is_returned_but_not_cached(tmp_path):
    device = FakeDevice("LP123", make_blob(baseline=500.0))

    _, source = calibration_cache.load_calibration(device, tmp_path)

    assert source == "device"
    assert not (tmp_path / "LP123.bin").exists()


def test_corrupt_cache_falls_back_to_the_device(tmp_path):
    (tmp_path / "LP123.bin").write_bytes(b"short")
    device = FakeDevice("LP123", make_blob())

    blob, source = calibration_cache.load_calibration(device, tmp_path)

    assert source == "device"
    assert (tmp_path / "LP123.bin").read_bytes() == blob


def test_unusable_serial_skips_the_cache(tmp_path):
    device = FakeDevice("", make_blob())

    _, source = calibration_cache.load_calibration(device, tmp_path)

    assert source == "device"
    assert list(tmp_path.iterdir()) == []


def test_serial_cannot_escape_the_cache_directory(tmp_path):
    assert calibration_cache.cache_path("../evil", tmp_path) is None

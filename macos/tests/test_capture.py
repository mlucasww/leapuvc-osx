import cv2
import numpy as np
import pytest

from leapmac import capture
from test_calibration_blob import make_blob


class FakeControls:
    def __init__(self, device):
        self.device = device

    def call(self, function):
        return function(self.device)


class RecordingDevice:
    def __init__(self):
        self.written = []

    def set(self, name, value):
        self.written.append((name, value))

    def get(self, name):
        return 21


def fake_thread():
    device = RecordingDevice()
    thread = capture.leapImageThread.__new__(capture.leapImageThread)
    thread.cam = capture.ControlChannel(FakeControls(device))
    return thread, device


def test_exposure_uses_the_inherited_method_and_its_minimum():
    thread, device = fake_thread()
    assert thread.setExposure(5)
    assert device.written == [("exposure", 10)]


def test_gain_and_gamma_map_to_their_controls():
    thread, device = fake_thread()
    thread.setGain(40)
    thread.setGammaEnabled(True)
    assert device.written == [("gain", 40), ("gamma", 1)]


def test_led_control_packs_selector_and_value():
    thread, device = fake_thread()
    thread.setLeftLED(True)
    thread.setRightLED(False)
    assert device.written == [("contrast", 2 | (1 << 6)), ("contrast", 4)]


def test_digital_gain_stays_in_the_device_range():
    thread, device = fake_thread()
    thread.setDigitalGain(0)
    thread.setDigitalGain(16)
    thread.setDigitalGain(8)
    assert device.written == [("digital_gain", 4), ("digital_gain", 15), ("digital_gain", 8)]


def test_unmapped_properties_are_refused():
    thread, device = fake_thread()
    assert thread.cam.set(cv2.CAP_PROP_FPS, 30) is False
    assert device.written == []


def test_get_reads_a_mapped_control():
    thread, _ = fake_thread()
    assert thread.cam.get(cv2.CAP_PROP_GAIN) == 21.0
    assert thread.cam.get(cv2.CAP_PROP_FPS) == 0.0


def test_control_failures_return_false_rather_than_raising():
    thread, _ = fake_thread()

    class Failing:
        def call(self, function):
            raise RuntimeError("device gone")

    thread.cam.controls = Failing()
    assert thread.setGain(20) is False


def test_vertical_zoom_is_refused():
    thread, device = fake_thread()
    with pytest.raises(NotImplementedError):
        thread.crashDevice(1)
    assert device.written == []


def test_embedded_line_matches_the_macos_decoder():
    from leapmac import frames

    thread, _ = fake_thread()
    thread.resolution = (8, 4)
    interleaved = np.zeros((4, 16), dtype=np.uint8)
    interleaved[-1, -12:] = [0, 0, 0x0A, 0, 0x03, 0, 0x1E, 0, 16, 0, 20, 0]

    label, exposure, gain = thread.getEmbeddedLine(interleaved)
    meta = frames.parse_embedded(interleaved)

    assert (label, exposure, gain) == (meta.frame_label, meta.exposure, meta.gain)


def parsed_calibration(monkeypatch):
    monkeypatch.setattr(capture._shared, "curve_fit", lambda *args, **kwargs: (np.zeros(6), None))
    return capture.parseLeapCalibration(make_blob(baseline=39.5), (640, 480))


def test_calibration_keeps_the_baseline(monkeypatch):
    assert parsed_calibration(monkeypatch)["baseline"] == pytest.approx(39.5)


def test_each_camera_uses_its_own_tangential_distortion(monkeypatch):
    calibration = parsed_calibration(monkeypatch)
    left = [float(value) for value in calibration["left"]["intrinsics"]["distCoeffs"][2:4]]
    right = [float(value) for value in calibration["right"]["intrinsics"]["distCoeffs"][2:4]]
    assert left == pytest.approx([3, 4])
    assert right == pytest.approx([23, 24])


def test_calibration_builds_maps_for_both_cameras(monkeypatch):
    calibration = parsed_calibration(monkeypatch)
    for camera in ("left", "right"):
        maps = calibration[camera]["undistortMaps"]
        assert maps[0].shape[:2] == (480, 640)

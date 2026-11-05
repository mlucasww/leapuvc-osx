import threading

import pytest

from leapmac.controls import ControlThread


class FakeUsb:
    def __init__(self, device):
        self.device = device

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def open(self, vendor_id, product_id):
        return self.device


class FakeDevice:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


def started(device):
    thread = ControlThread(1, 2, libusb_factory=lambda: FakeUsb(device))
    thread.open()
    return thread


def test_calls_run_on_the_control_thread():
    thread = started(FakeDevice())
    assert thread.call(lambda device: threading.current_thread().name) == "leap-controls"
    thread.stop()


def test_calls_run_in_order():
    thread = started(FakeDevice())
    seen = []
    for value in range(5):
        thread.call(lambda device, value=value: seen.append(value))
    assert seen == [0, 1, 2, 3, 4]
    thread.stop()


def test_errors_reach_the_caller_and_the_thread_survives():
    thread = started(FakeDevice())

    def fail(device):
        raise ValueError("bad control")

    with pytest.raises(ValueError):
        thread.call(fail)
    assert thread.call(lambda device: 42) == 42
    thread.stop()


def test_missing_device_fails_open():
    thread = ControlThread(1, 2, libusb_factory=lambda: FakeUsb(None))
    with pytest.raises(RuntimeError):
        thread.open()

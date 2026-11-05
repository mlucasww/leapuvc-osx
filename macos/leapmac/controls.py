"""Run every USB control request on one thread.

libusb handles aren't shared between callers here. UI callbacks, example code
and the capture thread all queue work, and this thread does it in order.
"""

import queue
import threading
from concurrent.futures import Future

from . import uvc_control

CALL_TIMEOUT_S = 3.0
OPEN_TIMEOUT_S = 5.0


class ControlThread(threading.Thread):
    def __init__(self, vendor_id, product_id, libusb_factory=uvc_control.Libusb):
        super().__init__(daemon=True, name="leap-controls")
        self.vendor_id = vendor_id
        self.product_id = product_id
        self.libusb_factory = libusb_factory
        self._work = queue.Queue()
        self._opened = Future()

    def open(self):
        """Start the thread and wait until the device is open. Raises if it can't open."""
        self.start()
        self._opened.result(timeout=OPEN_TIMEOUT_S)

    def run(self):
        try:
            with self.libusb_factory() as usb:
                device = usb.open(self.vendor_id, self.product_id)
                if device is None:
                    raise RuntimeError(f"no USB device {self.vendor_id:04x}:{self.product_id:04x}")
                with device:
                    self._opened.set_result(True)
                    self._serve(device)
        except Exception as error:
            if not self._opened.done():
                self._opened.set_exception(error)
            self._fail_remaining(error)

    def _serve(self, device):
        while True:
            item = self._work.get()
            if item is None:
                return
            function, future = item
            try:
                future.set_result(function(device))
            except Exception as error:
                future.set_exception(error)

    def _fail_remaining(self, error):
        while True:
            try:
                item = self._work.get_nowait()
            except queue.Empty:
                return
            if item is not None:
                item[1].set_exception(error)

    def call(self, function, timeout=CALL_TIMEOUT_S):
        """Run function(device) on this thread and return its result."""
        future = Future()
        self._work.put((function, future))
        return future.result(timeout=timeout)

    def stop(self):
        self._work.put(None)
        if self.is_alive():
            self.join(timeout=CALL_TIMEOUT_S)

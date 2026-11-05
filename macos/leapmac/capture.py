"""The LeapUVC Python API, running natively on macOS.

`leapImageThread` here keeps the public behaviour of the class in
Python/leapuvc.py, and inherits its control methods and metadata parsing. Only
the transport changes:

  frames     come from the leapcap helper through a shared-memory ring
  controls   go over USB endpoint 0 on one dedicated thread
  calibration is cached per device, and read before streaming starts

tools/run_example.py runs the repo's unchanged examples on top of this.
"""

import concurrent.futures
import importlib.util
import shutil
import tempfile
import threading
import time
from pathlib import Path

import cv2
import numpy as np

from . import calibration_cache, frames, uvc_control
from .controls import ControlThread
from .devices import DEFAULT_PID, DEFAULT_VID, matches_leap, run_leapcap_list
from .helper import StreamHelper
from .ring import FrameRing

MACOS_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = MACOS_ROOT.parent

READY_TIMEOUT_S = 15
FIRST_FRAME_TIMEOUT_S = 10
POLL_S = 0.001

CONTROL_ERRORS = (
    uvc_control.LibusbError,
    ValueError,
    RuntimeError,
    concurrent.futures.TimeoutError,
)

# cv2 property names that the shared code uses, mapped to their UVC controls.
PROPERTY_CONTROLS = {
    cv2.CAP_PROP_ZOOM: "exposure",
    cv2.CAP_PROP_GAIN: "gain",
    cv2.CAP_PROP_BRIGHTNESS: "digital_gain",
    cv2.CAP_PROP_CONTRAST: "contrast",
    cv2.CAP_PROP_GAMMA: "gamma",
    cv2.CAP_PROP_SHARPNESS: "sharpness",
    cv2.CAP_PROP_SATURATION: "saturation",
}


def _load_shared_module():
    path = REPOSITORY / "Python" / "leapuvc.py"
    spec = importlib.util.spec_from_file_location("leapuvc_shared", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_shared = _load_shared_module()
allLeapsRunning = _shared.allLeapsRunning
CayleyTransform = _shared.CayleyTransform
parseLeapCalibration = _shared.parseLeapCalibration


class LeapStreamError(RuntimeError):
    """The Leap could not be started, with a message saying what to check."""


class ControlChannel:
    """Stands in for cv2.VideoCapture, so the inherited control methods work unchanged."""

    def __init__(self, controls):
        self.controls = controls

    def isOpened(self):
        return True

    def set(self, prop, value):
        name = PROPERTY_CONTROLS.get(prop)
        if name is None:
            return False
        value = int(value) & 0xFFFF
        try:
            self.controls.call(lambda device: device.set(name, value))
        except CONTROL_ERRORS as error:
            print(f"Could not set {name}: {error}")
            return False
        return True

    def get(self, prop, value=None):
        name = PROPERTY_CONTROLS.get(prop)
        if name is None:
            return 0.0
        try:
            return float(self.controls.call(lambda device: device.get(name)))
        except CONTROL_ERRORS as error:
            print(f"Could not read {name}: {error}")
            return 0.0


def find_unique_id(leapcap, source, vendor_id, product_id):
    listing = run_leapcap_list(leapcap)
    if listing is None:
        raise LeapStreamError(f"{leapcap} produced no device list. Run `make leapcap` first.")
    if listing.get("authorization") != "authorized":
        raise LeapStreamError(
            f"Camera access is {listing.get('authorization')}. Grant it to the app running this "
            "in System Settings > Privacy & Security > Camera, or run from Terminal.app."
        )
    leaps = [device for device in listing["devices"] if matches_leap(device, vendor_id, product_id)]
    if source >= len(leaps):
        raise LeapStreamError(f"Leap {source} not found. AVFoundation lists {len(leaps)} Leap(s).")
    return leaps[source]["unique_id"]


class leapImageThread(_shared.leapImageThread):
    """A dedicated thread that retrieves imagery from an unlocked Leap on macOS."""

    def __init__(
        self,
        source=0,
        resolution=(640, 480),
        timeout=3.0,
        leapcap=None,
        vendorId=DEFAULT_VID,
        productId=DEFAULT_PID,
        cacheDirectory=calibration_cache.DEFAULT_DIRECTORY,
    ):
        # The parent's __init__ would open cv2.VideoCapture, so skip past it.
        threading.Thread.__init__(self)
        self.source = source
        self.resolution = (int(resolution[0]), int(resolution[1]))
        self.frame = None
        self.newFrame = False
        self.timeoutTimer = time.time()
        self.timeout = timeout
        self.cameras = ["left", "right"]
        self.embeddedLine = None
        self.doYUYConversion = False

        self.frameListener = None
        self.framesReceived = 0
        self.framesSkipped = 0
        self.firstSequence = 0
        self.lastSequence = 0

        self.helper = None
        self.ring = None
        self.controls = None
        self.cam = None
        self._stopRequested = False
        self._closed = False
        self._workDirectory = tempfile.mkdtemp(prefix="leapmac-")

        leapcap = Path(leapcap) if leapcap else MACOS_ROOT / "build" / "leapcap"
        try:
            self.calibration, self.calibrationSource = self._readCalibration(
                vendorId, productId, cacheDirectory
            )
            uniqueId = find_unique_id(leapcap, source, vendorId, productId)
            self._startStreaming(leapcap, uniqueId)
            # Open USB only now: holding it while streaming starts may conflict.
            self.controls = ControlThread(vendorId, productId)
            self.controls.open()
        except Exception:
            self._shutdown()
            raise
        self.cam = ControlChannel(self.controls)
        self.running = True

    def _readCalibration(self, vendorId, productId, cacheDirectory):
        with uvc_control.Libusb() as usb:
            device = usb.open(vendorId, productId)
            if device is None:
                raise LeapStreamError(f"No USB device {vendorId:04x}:{productId:04x}. Is the Leap plugged in?")
            with device:
                blob, origin = calibration_cache.load_calibration(device, cacheDirectory)
        return parseLeapCalibration(blob, self.resolution), origin

    def _startStreaming(self, leapcap, uniqueId):
        width, height = self.resolution
        ringPath = Path(self._workDirectory) / "frames.ring"
        self.helper = StreamHelper(leapcap, uniqueId, width, height, ringPath)
        self.helper.start()

        event = self.helper.wait_for("ready", READY_TIMEOUT_S)
        if event is None or event.get("event") == "error":
            detail = event["message"] if event else "it reported nothing"
            raise LeapStreamError(f"leapcap could not start streaming: {detail}")

        self.ring = FrameRing(ringPath)
        self.pixelFormat = self.ring.pixel_format
        deadline = time.monotonic() + FIRST_FRAME_TIMEOUT_S
        while self.ring.latest_sequence() == 0:
            if time.monotonic() > deadline or not self.helper.alive():
                raise LeapStreamError(
                    "The Leap sent no frames. Check that it's unlocked: on Windows, run "
                    "UnlockDevice.exe as administrator, then confirm the Camera app shows an image."
                )
            time.sleep(POLL_S)

    def run(self):
        self.running = True
        self.timeoutTimer = time.time()
        width, height = self.resolution
        try:
            while time.time() - self.timeoutTimer < self.timeout and not self._stopRequested:
                frame = self.ring.read_next(self.lastSequence)
                if frame is None:
                    if not self.helper.alive():
                        print("The leapcap helper stopped unexpectedly.")
                        break
                    time.sleep(POLL_S)
                    continue
                self._publish(frame, width, height)
        finally:
            print("Exiting Leap Image Thread!")
            self.running = False
            self._shutdown()

    def _publish(self, frame, width, height):
        if self.lastSequence:
            self.framesSkipped += frame.sequence - self.lastSequence - 1
        else:
            self.firstSequence = frame.sequence
        self.lastSequence = frame.sequence
        self.framesReceived += 1

        interleaved = frames.to_interleaved(frame.payload, width, height, self.pixelFormat)
        self.embeddedLine = self.getEmbeddedLine(interleaved)
        left, right = frames.deinterleave(interleaved)
        leftRightImage = np.empty((2, height, width), dtype=np.uint8)
        leftRightImage[0, :, :] = left
        leftRightImage[1, :, :] = right
        self.frame = leftRightImage
        self.newFrame = True
        if self.frameListener is not None:
            self.frameListener(frame.sequence, frame.pts_ns, self.embeddedLine, leftRightImage)

    def stop(self):
        """Ask the capture thread to finish. Join the thread to wait for it."""
        self._stopRequested = True

    def crashDevice(self, value):
        raise NotImplementedError("Vertical zoom crashes the device, so it isn't exposed on macOS.")

    def _shutdown(self):
        if self._closed:
            return
        self._closed = True
        if self.controls is not None:
            self.controls.stop()
        if self.helper is not None:
            self.helper.stop()
        if self.ring is not None:
            self.torn = self.ring.torn
            self.ring.close()
        shutil.rmtree(self._workDirectory, ignore_errors=True)

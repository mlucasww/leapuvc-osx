"""Send UVC control requests to the Leap over USB endpoint 0.

macOS's UVC driver owns the streaming interface, so claiming an interface
needs root. Class requests on the default control pipe need no claim, and they
work alongside AVFoundation streaming.

This binds libusb directly through ctypes. PyUSB is avoided because its
ctrl_transfer tries to claim the interface for class requests addressed to an
interface, and that claim is what macOS refuses.
"""

import ctypes
import ctypes.util
import os
import time
from ctypes import POINTER, byref, c_char_p, c_int, c_ssize_t, c_ubyte, c_uint, c_ushort, c_void_p
from dataclasses import dataclass

from .uvc_descriptors import parse_configuration

# Standard requests
REQUEST_TYPE_DEVICE_IN = 0x80
REQUEST_GET_DESCRIPTOR = 0x06
DESCRIPTOR_CONFIGURATION = 0x02

# UVC class requests addressed to an interface
REQUEST_TYPE_CLASS_OUT = 0x21
REQUEST_TYPE_CLASS_IN = 0xA1
SET_CUR = 0x01
GET_CUR = 0x81
GET_MIN = 0x82
GET_MAX = 0x83
GET_DEF = 0x87

# UVC 1.0 VideoStreaming controls, addressed to the streaming interface
VS_PROBE_CONTROL = 0x01
VS_COMMIT_CONTROL = 0x02
VS_STREAM_ERROR_CODE_CONTROL = 0x06
VS_CONTROL_LENGTH = 26

TIMEOUT_MS = 1000

CALIBRATION_ADDRESSES = range(100, 256)
CALIBRATION_DELAY_S = 0.004

# Registers that hold what looks like an ASCII serial number. leapd reads these.
SERIAL_ADDRESSES = range(0x00, 0x0E)

# Name: (unit kind, selector). All of these controls are 2 bytes long.
CONTROLS = {
    "exposure": ("camera", 0x0B),  # CT_ZOOM_ABSOLUTE
    "digital_gain": ("processing", 0x02),  # PU_BRIGHTNESS
    "contrast": ("processing", 0x03),  # PU_CONTRAST, multiplexed
    "gain": ("processing", 0x04),  # PU_GAIN
    "saturation": ("processing", 0x07),  # PU_SATURATION, calibration data
    "sharpness": ("processing", 0x08),  # PU_SHARPNESS, calibration address
    "gamma": ("processing", 0x09),  # PU_GAMMA
    "dark_frame_interval": ("processing", 0x0A),  # PU_WHITE_BALANCE_TEMPERATURE
}

# Selectors for the multiplexed contrast control. Vertical zoom (6) crashes the
# device, so it is deliberately left out.
CONTRAST_HDR = 0
CONTRAST_ROTATE = 1
CONTRAST_LED_LEFT = 2
CONTRAST_LED_CENTER = 3
CONTRAST_LED_RIGHT = 4
CONTRAST_VCENTER = 5

GAIN_SELECTOR = 0x4000

SPEED_NAMES = {0: "unknown", 1: "low", 2: "full", 3: "high", 4: "super", 5: "super+"}


class LibusbError(RuntimeError):
    def __init__(self, code, name, context):
        super().__init__(f"{context}: {name} ({code})")
        self.code = code
        self.name = name


class _DeviceDescriptor(ctypes.Structure):
    _fields_ = [
        ("bLength", c_ubyte),
        ("bDescriptorType", c_ubyte),
        ("bcdUSB", c_ushort),
        ("bDeviceClass", c_ubyte),
        ("bDeviceSubClass", c_ubyte),
        ("bDeviceProtocol", c_ubyte),
        ("bMaxPacketSize0", c_ubyte),
        ("idVendor", c_ushort),
        ("idProduct", c_ushort),
        ("bcdDevice", c_ushort),
        ("iManufacturer", c_ubyte),
        ("iProduct", c_ubyte),
        ("iSerialNumber", c_ubyte),
        ("bNumConfigurations", c_ubyte),
    ]


def find_libusb():
    """Locate the libusb dylib, preferring Homebrew's location on Apple Silicon."""
    candidates = [
        os.environ.get("LIBUSB_PATH"),
        "/opt/homebrew/lib/libusb-1.0.dylib",
        "/opt/homebrew/opt/libusb/lib/libusb-1.0.dylib",
        "/usr/local/lib/libusb-1.0.dylib",
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return path
    found = ctypes.util.find_library("usb-1.0")
    if found:
        return found
    raise FileNotFoundError("libusb not found: run `brew install libusb` or set LIBUSB_PATH")


def _bind(lib):
    bindings = [
        ("libusb_init", c_int, [POINTER(c_void_p)]),
        ("libusb_exit", None, [c_void_p]),
        ("libusb_get_device_list", c_ssize_t, [c_void_p, POINTER(POINTER(c_void_p))]),
        ("libusb_free_device_list", None, [POINTER(c_void_p), c_int]),
        ("libusb_get_device_descriptor", c_int, [c_void_p, POINTER(_DeviceDescriptor)]),
        ("libusb_open", c_int, [c_void_p, POINTER(c_void_p)]),
        ("libusb_close", None, [c_void_p]),
        ("libusb_reset_device", c_int, [c_void_p]),
        (
            "libusb_control_transfer",
            c_int,
            [c_void_p, c_ubyte, c_ubyte, c_ushort, c_ushort, POINTER(c_ubyte), c_ushort, c_uint],
        ),
        ("libusb_get_string_descriptor_ascii", c_int, [c_void_p, c_ubyte, POINTER(c_ubyte), c_int]),
        ("libusb_get_bus_number", c_ubyte, [c_void_p]),
        ("libusb_get_port_number", c_ubyte, [c_void_p]),
        ("libusb_get_device_address", c_ubyte, [c_void_p]),
        ("libusb_get_device_speed", c_int, [c_void_p]),
        ("libusb_error_name", c_char_p, [c_int]),
    ]
    for name, restype, argtypes in bindings:
        function = getattr(lib, name)
        function.restype = restype
        function.argtypes = argtypes


@dataclass
class UsbDeviceInfo:
    vendor_id: int
    product_id: int
    bus: int
    port: int
    address: int
    speed: str
    manufacturer: str = ""
    product: str = ""
    serial: str = ""
    is_uvc: bool = False
    open_error: str = ""


class Libusb:
    """Owns the libusb library and its context."""

    def __init__(self, path=None):
        self.path = path or find_libusb()
        self.lib = ctypes.CDLL(self.path)
        _bind(self.lib)
        self.context = c_void_p()
        self.check(self.lib.libusb_init(byref(self.context)), "libusb_init")

    def close(self):
        if self.context:
            self.lib.libusb_exit(self.context)
            self.context = c_void_p()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def check(self, code, context):
        if code < 0:
            raise LibusbError(code, self.error_name(code), context)
        return code

    def error_name(self, code):
        name = self.lib.libusb_error_name(code)
        return name.decode("ascii") if name else "UNKNOWN"

    def _devices(self):
        listing = POINTER(c_void_p)()
        count = self.lib.libusb_get_device_list(self.context, byref(listing))
        self.check(count, "libusb_get_device_list")
        return listing, count

    def _descriptor(self, device):
        descriptor = _DeviceDescriptor()
        self.check(
            self.lib.libusb_get_device_descriptor(device, byref(descriptor)),
            "libusb_get_device_descriptor",
        )
        return descriptor

    def _info(self, device, descriptor):
        return UsbDeviceInfo(
            vendor_id=descriptor.idVendor,
            product_id=descriptor.idProduct,
            bus=self.lib.libusb_get_bus_number(device),
            port=self.lib.libusb_get_port_number(device),
            address=self.lib.libusb_get_device_address(device),
            speed=SPEED_NAMES.get(self.lib.libusb_get_device_speed(device), "unknown"),
        )

    def _open(self, device, descriptor):
        handle = c_void_p()
        self.check(self.lib.libusb_open(device, byref(handle)), "libusb_open")
        return UvcDevice(self, handle, self._info(device, descriptor), descriptor)

    def list_devices(self):
        """Describe every USB device, opening each one to see if it is UVC.

        libusb's open tries to seize the device on macOS, so only use this as a
        diagnostic when the Leap cannot be found, not on every run.
        """
        listing, count = self._devices()
        results = []
        try:
            for index in range(count):
                device = listing[index]
                descriptor = self._descriptor(device)
                try:
                    with self._open(device, descriptor) as opened:
                        results.append(opened.info)
                except (LibusbError, ValueError) as error:
                    info = self._info(device, descriptor)
                    info.open_error = str(error)
                    results.append(info)
        finally:
            self.lib.libusb_free_device_list(listing, 1)
        return results

    def open(self, vendor_id, product_id):
        """Open the first device with this VID and PID, or return None.

        Do not hold the handle while AVFoundation starts streaming, because
        libusb's seize may conflict with it. Open again once frames are flowing.
        """
        listing, count = self._devices()
        try:
            for index in range(count):
                device = listing[index]
                descriptor = self._descriptor(device)
                if descriptor.idVendor == vendor_id and descriptor.idProduct == product_id:
                    return self._open(device, descriptor)
        finally:
            self.lib.libusb_free_device_list(listing, 1)
        return None


class UvcDevice:
    """An open device. Requests go over endpoint 0 and no interface is claimed."""

    def __init__(self, usb, handle, info, descriptor):
        self.usb = usb
        self.handle = handle
        self.info = info
        self.closed = False
        try:
            self.info.manufacturer = self._string(descriptor.iManufacturer)
            self.info.product = self._string(descriptor.iProduct)
            self.info.serial = self._string(descriptor.iSerialNumber)
            self.topology = parse_configuration(self.read_configuration())
            self.info.is_uvc = self.topology.is_uvc
        except Exception:
            self.close()
            raise

    def close(self):
        if not self.closed:
            self.usb.lib.libusb_close(self.handle)
            self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _string(self, index):
        if not index:
            return ""
        buffer = (c_ubyte * 256)()
        length = self.usb.lib.libusb_get_string_descriptor_ascii(self.handle, index, buffer, 256)
        if length < 0:
            return ""
        return bytes(buffer[:length]).decode("ascii", errors="replace")

    def _transfer(self, request_type, request, value, index, buffer, length, context):
        transferred = self.usb.lib.libusb_control_transfer(
            self.handle, request_type, request, value, index, buffer, length, TIMEOUT_MS
        )
        return self.usb.check(transferred, context)

    def read_configuration(self):
        """Read the raw configuration descriptor over the default pipe."""
        value = DESCRIPTOR_CONFIGURATION << 8
        header = (c_ubyte * 9)()
        self._transfer(
            REQUEST_TYPE_DEVICE_IN, REQUEST_GET_DESCRIPTOR, value, 0, header, 9, "read config header"
        )
        total = header[2] | (header[3] << 8)
        full = (c_ubyte * total)()
        received = self._transfer(
            REQUEST_TYPE_DEVICE_IN, REQUEST_GET_DESCRIPTOR, value, 0, full, total, "read config"
        )
        return bytes(full[:received])

    def _unit(self, kind):
        topology = self.topology
        unit = topology.camera_terminal if kind == "camera" else topology.processing_unit
        if unit is None:
            raise ValueError(f"device descriptors have no {kind} unit")
        return unit

    def _control_address(self, name):
        kind, selector = CONTROLS[name]
        return selector << 8, (self._unit(kind) << 8) | self.topology.vc_interface

    def _get(self, request, name):
        value, index = self._control_address(name)
        buffer = (c_ubyte * 2)()
        received = self._transfer(
            REQUEST_TYPE_CLASS_IN, request, value, index, buffer, 2, f"GET {name}"
        )
        return int.from_bytes(bytes(buffer[:received]), "little")

    def get(self, name):
        return self._get(GET_CUR, name)

    def get_range(self, name):
        """Return (min, max, default) as the device reports them."""
        return self._get(GET_MIN, name), self._get(GET_MAX, name), self._get(GET_DEF, name)

    def set(self, name, value):
        address, index = self._control_address(name)
        buffer = (c_ubyte * 2)(value & 0xFF, (value >> 8) & 0xFF)
        self._transfer(REQUEST_TYPE_CLASS_OUT, SET_CUR, address, index, buffer, 2, f"SET {name}")

    def write_raw(self, request_type, request, value, index, data=b""):
        """Send one control write exactly as given, for replaying recorded traffic."""
        buffer = (c_ubyte * len(data))(*data)
        self._transfer(request_type, request, value, index, buffer, len(data), "raw control write")

    def reset(self):
        """Ask for a USB reset. The device may re-enumerate, so reopen it afterwards."""
        self.usb.check(self.usb.lib.libusb_reset_device(self.handle), "libusb_reset_device")

    def read_stream_state(self):
        """Read the device's negotiated streaming state over endpoint 0.

        Failures are returned as text, because a refused read is itself evidence.
        """
        state = {}
        requests = (
            ("commit", VS_COMMIT_CONTROL, VS_CONTROL_LENGTH),
            ("probe", VS_PROBE_CONTROL, VS_CONTROL_LENGTH),
            ("stream_error", VS_STREAM_ERROR_CODE_CONTROL, 1),
        )
        for name, selector, length in requests:
            buffer = (c_ubyte * length)()
            try:
                received = self._transfer(
                    REQUEST_TYPE_CLASS_IN,
                    GET_CUR,
                    selector << 8,
                    self.topology.vs_interface,
                    buffer,
                    length,
                    f"GET {name}",
                )
                state[name] = bytes(buffer[:received]).hex()
            except LibusbError as error:
                state[name] = str(error)
        return state

    def set_contrast_field(self, selector, value):
        """Set one field of the multiplexed contrast control."""
        self.set("contrast", selector | (value << 6))

    def set_leds(self, enabled):
        for selector in (CONTRAST_LED_LEFT, CONTRAST_LED_CENTER, CONTRAST_LED_RIGHT):
            self.set_contrast_field(selector, 1 if enabled else 0)

    def read_calibration_bytes(self):
        """Run the sharpness/saturation knock and return the 156-byte blob.

        Returns (blob, high_bytes_seen). The second value is the number of
        reads that carried a nonzero high byte, which would be unexpected.
        """
        values = self.read_registers(CALIBRATION_ADDRESSES)
        high_bytes_seen = sum(1 for value in values if value > 0xFF)
        return bytes(value & 0xFF for value in values), high_bytes_seen

    def read_serial(self):
        """Return the ASCII text at the serial registers, or "" if there is none."""
        values = self.read_registers(SERIAL_ADDRESSES)
        text = bytes(value & 0xFF for value in values).split(b"\0")[0]
        decoded = text.decode("ascii", errors="ignore")
        return decoded if decoded.isalnum() else ""

    def read_registers(self, addresses):
        """Read through the sharpness/saturation channel. This sends no data writes.

        Selecting an address is itself a control write, and some addresses may
        act as commands, so only pass addresses known to be safe to select.
        """
        values = []
        for address in addresses:
            self.set("sharpness", address)
            time.sleep(CALIBRATION_DELAY_S)
            values.append(self.get("saturation"))
        return values

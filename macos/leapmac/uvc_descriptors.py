"""Parse a USB configuration descriptor to find a UVC device's layout.

UVC control requests are addressed by unit ID, and those IDs vary by device,
so they are read from the descriptors instead of being hard-coded.
"""

from dataclasses import dataclass, field

DESCRIPTOR_INTERFACE = 0x04
DESCRIPTOR_CS_INTERFACE = 0x24

CLASS_VIDEO = 0x0E
SUBCLASS_VIDEO_CONTROL = 0x01
SUBCLASS_VIDEO_STREAMING = 0x02

VC_INPUT_TERMINAL = 0x02
VC_PROCESSING_UNIT = 0x05
VC_EXTENSION_UNIT = 0x06
TERMINAL_CAMERA = 0x0201

VS_FORMAT_UNCOMPRESSED = 0x04
VS_FRAME_UNCOMPRESSED = 0x05


@dataclass
class UvcFrame:
    width: int
    height: int
    default_interval_100ns: int
    intervals_100ns: list


@dataclass
class UvcFormat:
    index: int
    fourcc: str
    bits_per_pixel: int
    frames: list = field(default_factory=list)


@dataclass
class UvcTopology:
    vc_interface: int = None
    camera_terminal: int = None
    processing_unit: int = None
    extension_units: list = field(default_factory=list)
    formats: list = field(default_factory=list)

    @property
    def is_uvc(self):
        return self.vc_interface is not None


def _u16(data, offset):
    return int.from_bytes(data[offset : offset + 2], "little")


def _u32(data, offset):
    return int.from_bytes(data[offset : offset + 4], "little")


def _parse_frame(descriptor):
    interval_type = descriptor[25]
    # A type of 0 means continuous: minimum, maximum and step.
    count = 3 if interval_type == 0 else interval_type
    intervals = [
        _u32(descriptor, 26 + 4 * index)
        for index in range(count)
        if 26 + 4 * index + 4 <= len(descriptor)
    ]
    return UvcFrame(
        width=_u16(descriptor, 5),
        height=_u16(descriptor, 7),
        default_interval_100ns=_u32(descriptor, 21),
        intervals_100ns=intervals,
    )


def parse_configuration(blob):
    """Parse a full configuration descriptor into a UvcTopology."""
    topology = UvcTopology()
    interface_class = None
    interface_subclass = None
    current_format = None

    offset = 0
    while offset + 2 <= len(blob):
        length = blob[offset]
        kind = blob[offset + 1]
        if length < 2 or offset + length > len(blob):
            break
        descriptor = bytes(blob[offset : offset + length])
        offset += length

        if kind == DESCRIPTOR_INTERFACE and length >= 9:
            interface_class = descriptor[5]
            interface_subclass = descriptor[6]
            if (
                interface_class == CLASS_VIDEO
                and interface_subclass == SUBCLASS_VIDEO_CONTROL
                and topology.vc_interface is None
            ):
                topology.vc_interface = descriptor[2]
            continue

        if kind != DESCRIPTOR_CS_INTERFACE or interface_class != CLASS_VIDEO or length < 4:
            continue

        subtype = descriptor[2]
        if interface_subclass == SUBCLASS_VIDEO_CONTROL:
            if subtype == VC_INPUT_TERMINAL and length >= 8:
                if _u16(descriptor, 4) == TERMINAL_CAMERA:
                    topology.camera_terminal = descriptor[3]
            elif subtype == VC_PROCESSING_UNIT:
                topology.processing_unit = descriptor[3]
            elif subtype == VC_EXTENSION_UNIT:
                topology.extension_units.append(descriptor[3])
        elif interface_subclass == SUBCLASS_VIDEO_STREAMING:
            if subtype == VS_FORMAT_UNCOMPRESSED and length >= 27:
                current_format = UvcFormat(
                    index=descriptor[3],
                    fourcc=descriptor[5:9].decode("ascii", errors="replace"),
                    bits_per_pixel=descriptor[21],
                )
                topology.formats.append(current_format)
            elif subtype == VS_FRAME_UNCOMPRESSED and length >= 26 and current_format is not None:
                current_format.frames.append(_parse_frame(descriptor))

    return topology

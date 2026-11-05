# macOS port: design notes and results

This records why the port is built the way it is and what testing found. Setup and run instructions are in [macos/README.md](../macos/README.md).

Diagrams of the architecture, frame pipeline, startup sequence, Phase 0 decisions, data structures and control map are on the [design canvas](https://claude.ai/artifact/F5Jw8hw83G4R3RoiXpKtsw). The link is private until shared.

## Why macOS never worked

`Python/leapuvc.py` does everything through `cv2.VideoCapture`, and OpenCV's Mac camera backend can't do what it needs:

- It always requests BGRA. The Leap's stream is labelled YUY2, but it is really two interleaved 8-bit images, so a colour conversion destroys them.
- Its `setProperty` handles only width, height and FPS. Exposure, gain and LEDs all fail, and so does the sharpness/saturation "knock" that reads calibration.

Using libuvc instead isn't a clean option, because streaming that way needs root or an entitlement Apple rarely grants since macOS 11.6.3.

## Design

Frames and controls take separate paths:

- **Frames:** AVFoundation in a Swift helper process. It requests `yuvs`, which should carry the raw bytes unchanged. Running outside Python means the GIL can't stall capture.
- **Controls:** UVC class requests on USB endpoint 0 through libusb, with no interface claimed. This needs no root and works alongside streaming.

The `macuvc` project proved this split on Apple Silicon, but it keeps only every other byte. On a Leap, that would lose the right camera.

Nobody has shown the Leap itself working this way, which is why Phase 0 tests it first.

## Decisions

| Topic | Decision |
|---|---|
| Purpose | Scene layout. The point cloud is the output, with no focus or depth-of-field maths |
| Mounting | Rigid on the mirrorless camera |
| Output | Display only. Tethering may come later |
| Stack | Python and OpenCV for processing, a Swift helper for capture, libusb for controls, Rerun for the 3D view |

## Implementation notes

- **Don't use PyUSB for controls.** As I recall, its `ctrl_transfer` claims the interface for class requests addressed to an interface, which macOS refuses. Confirm this if PyUSB is ever considered again.
- **Don't hold a USB handle while streaming starts.** libusb's open tries to seize the device, which may conflict with AVFoundation. The probe opens the device again after the first frame arrives.
- **Metadata wraps.** Exposure in the embedded line repeats every 512 and gain repeats every 32, so compare them modulo those values.
- **Gain is documented two ways.** The manual uses a selector form (`0x4000 | value`), while the repo code writes the plain value. The probe tests both.

## Known issues in the existing code

These will be fixed in Phase 1:

- `Python/leapuvc.py:204` uses the left camera's tangential distortion for both cameras.
- `calibrationArray[5]`, the baseline, is parsed but never stored. Metric depth needs it.
- `Python/leapuvc.py:211` rectifies each camera to its own matrix. That's fine for a preview, but metric depth needs one shared matrix.

## Phase status

| Phase | Status |
|---|---|
| 0. Prove the path on hardware | Code written, not yet run |
| 1. Capture and control port | Not started |
| 2. Metric stereo | Not started |
| 3. Live point cloud | Not started |
| 4. Camera mount | Not started |
| 5. Documentation and handover | Not started |

## Results

No hardware results yet. Probe findings, measured frame rates and the depth error table will go here.

## Sources

- [OpenCV AVFoundation backend](https://github.com/opencv/opencv/blob/master/modules/videoio/src/cap_avfoundation_mac.mm)
- [macuvc / uvc-avf](https://github.com/VimalMollyn/uvc-avf)
- [libusb darwin backend](https://github.com/libusb/libusb/blob/master/libusb/os/darwin_usb.c)
- [CameraController](https://github.com/itaybre/CameraController)
- [purethermal1-uvc-capture issue #56](https://github.com/groupgets/purethermal1-uvc-capture/issues/56)

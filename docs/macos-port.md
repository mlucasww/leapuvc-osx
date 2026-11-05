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
| 0. Prove the path on hardware | In progress: controls work, no frames yet |
| 1. Capture and control port | Not started |
| 2. Metric stereo | Not started |
| 3. Live point cloud | Not started |
| 4. Camera mount | Not started |
| 5. Documentation and handover | Not started |

## Results

### First hardware run, 2026-11-04

Tested on macOS 15.8.1, arm64, with the Leap connected directly at USB 2.0 high speed.

**Working without root:**

- `leapcap` builds cleanly, and all 34 unit tests pass.
- Calibration reads identically twice, using endpoint 0 control requests with no interface claimed.
- Baseline is 39.87 mm. Focal lengths are 133.8 px (left) and 134.2 px (right).
- AVFoundation lists the Leap with `yuvs` at every resolution. Its unique ID encodes the USB location, so it changes between ports.

**Device-reported modes:**

| Resolution | Maximum FPS |
|---|---|
| 640×480 | 57.5 |
| 640×240 | 115 |
| 640×120 | 214 |
| 752×480 | 50 |
| 752×240 | 100 |
| 752×120 | 190 |

**Not working: no frames.** The AVFoundation session starts and macOS's UVC driver reads the stream, but it logs `TotalPackets: 0`. The Leap isn't sending anything, so neither `leapcap` nor Apple's frame processing is dropping data. This held across five streams, at 640×480 and 640×120, and after a clean replug into a different port with no libusb access.

**Device details learned:**

- The streaming interface is **bulk**, on endpoint `0x83` with 512-byte packets and alternate setting 0, not isochronous.
- `dwMaxVideoFrameSize` reads 721920 for every mode, and `dwClockFrequency` reads 0.
- `bcdDevice` is 0, so the descriptors don't reveal the firmware version.
- Units: camera terminal 2, processing unit 5, extension unit 6.

**Likely cause, not yet proven:** the OpenMotion project reports this same device (`f182:0003`, firmware 1.7.0) boots inert, with the same symptoms, until a sequence of control writes brings it up. Those writes travel on endpoint 0, which we've shown works without root. The manual says firmware before 1.7.1 needs authentication, so a device that isn't actually unlocked would behave like this. OpenMotion also reports a later regression where the same replay lit the LEDs but returned no bulk data, so bring-up alone may not be enough.

**Probe flaw found:** it recommended Path C because zero frames counted as a failure, though nothing showed AVFoundation was at fault. It now reports that as undecided.

## Sources

- [OpenCV AVFoundation backend](https://github.com/opencv/opencv/blob/master/modules/videoio/src/cap_avfoundation_mac.mm)
- [macuvc / uvc-avf](https://github.com/VimalMollyn/uvc-avf)
- [libusb darwin backend](https://github.com/libusb/libusb/blob/master/libusb/os/darwin_usb.c)
- [CameraController](https://github.com/itaybre/CameraController)
- [purethermal1-uvc-capture issue #56](https://github.com/groupgets/purethermal1-uvc-capture/issues/56)
- [OpenMotion](https://github.com/coleman-sagil/OpenMotion)
- [wtfuzz/leapuvc](https://github.com/wtfuzz/leapuvc)

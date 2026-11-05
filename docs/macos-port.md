# macOS port: design notes and results

This records why the port is built the way it is and what testing found. Setup and run instructions are in [macos/README.md](../macos/README.md).

## Restarting this work

**Where it stands:** Phases 0 and 1 are complete. The Leap streams natively on Apple Silicon at 57.55 FPS with no root, and the Python library and examples work. Phase 2, metric stereo, is next and hasn't been started.

**Ground rules, which still apply:**

- Don't compile or run code on the development MacBook. Write code there, then build and run on the Sequoia test Mac.
- Start hardware tests from Terminal.app on the test Mac, because camera permission follows the launching app.
- The test Mac keeps the repo at `/Users/wildwood/dev/leapuvc-osx`, on macOS 15.8.1. Its Claude session reaches this one through Remote Control.
- Don't rerun `tools/bringup_test.py`. Don't install Leap or Ultraleap software on anything this unit connects to, because nobody has tested whether it would undo the unlock.
- If a Leap sends no frames, confirm the unlock first: `UnlockDevice.exe` must run as administrator, and the Windows Camera app should show an image.

**To restart:**

1. On the test Mac, get `master` and rebuild, from the `macos/` folder:

   ```bash
   git switch master && git pull
   ```

   ```bash
   make setup && make leapcap && make test
   ```

   Expect 88 tests to pass.

2. Run a quick health check, which should report a clean run at about 57.55 FPS:

   ```bash
   .venv/bin/python tools/stream_test.py --seconds 10
   ```

**Next: Phase 2, metric stereo**

1. Add `macos/leapmac/stereo.py`, building a `StereoRig` from the calibration: one shared rectified camera matrix for both cameras, plus `Q` from the 39.87 mm baseline. `parseLeapCalibration` still gives each camera its own matrix, which is fine for a preview but wrong for metric depth.
2. Compute disparity with `cv2.StereoSGBM` at 640×480, and convert it to metres.
3. Run a tape-measure test at 0.5, 1, 2 and 3 m, using a flat, textured target. Publish the measured error table, replacing the estimates in the plan.

**Phase 2 needs from the user:** a tape measure, a flat textured target, and someone at the test Mac.

**Keep in mind for Phase 2:**

- Mount the Leap with its long edge horizontal and the green light at the bottom.
- Prefer gain over long exposure, because exposure has a weak effect and long values cut the frame rate.
- Skip the first second of brightness data and the first frame's exposure metadata.
- Rectification looks right by eye, but its metric accuracy is unproven.

**Later phases:** Phase 3 needs Python 3.11 or later on the test Mac for Rerun, and it has 3.9.6. Phase 3 is also the first test of stereo processing alongside capture. Phase 4 adds the camera mount, and Phase 5 finishes the documentation. Tethered camera control stays out of scope for now.

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

Phase 0 confirmed this works on an unlocked Leap, with both cameras' bytes intact.

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

Fixed in Phase 1, which changes `Python/leapuvc.py` on every platform:

- The right camera used the left camera's tangential distortion. Each camera now uses its own, which slightly changes the right camera's rectification.
- The baseline was parsed but never stored. It's now `calibration["baseline"]`.
- `cv2.CV_8UC1` was passed as the undistortion map type, and only worked because OpenCV treats 0 as its default. It's now `cv2.CV_16SC2` explicitly.

Still to fix in Phase 2: `parseLeapCalibration` rectifies each camera to its own matrix. That's fine for a preview, but metric depth needs one shared matrix.

## Phase status

| Phase | Status |
|---|---|
| 0. Prove the path on hardware | Done: Path A confirmed |
| 1. Capture and control port | Done |
| 2. Metric stereo | Code written, not yet run |
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

### Bring-up replay, 2026-11-04

OpenMotion's 358 recorded control writes were replayed before capture, during capture, and around a USB reset. Every write reported success, and none of the three runs produced a packet.

- **Negotiation matches the recording.** During capture, the device's commit state matched the recorded commit from byte 2 on, and the stream error code stayed 0. macOS's own commit reaches the device, so negotiation isn't the obvious gap.
- **IR LEDs glowed in all three runs.** This hasn't been compared with a capture without a replay, so the replay isn't proven to be the cause.
- **macOS intercepts `SET_INTERFACE`.** The kernel logs "intercepting manually crafted SET_INTERFACE request", and libusb reports success, so those rows may not have reached the device as recorded.
- **`libusb_reset_device` works without root.** The Leap re-enumerated and was reopened.
- **`dwMaxVideoFrameSize` of 721920 is normal.** The recorded commit carries the same value.

**The replay is probably the wrong tool for this unit.** The sequence writes 16 high-entropy bytes through the saturation control to addresses 16 to 19, 32 to 39 and 44 to 47. The wtfuzz recording writes different bytes to the same addresses, which suggests an authentication response tied to one device or session. OpenMotion's regression, with LEDs lit and no bulk data, also matches what we saw.

**Correction:** before the replay we described the worst case as a crash that a replug clears. That wasn't established, because nobody knows whether those addresses are persistent. The replay should not be repeated without a specific reason.

### Root cause: the Leap was never unlocked

The zero-packet failure wasn't a macOS problem. The test unit hadn't been unlocked.

- `UnlockDevice.exe` was first run without administrator rights. It opened a shell, closed after Enter was pressed, and gave no sign that nothing had happened.
- Before the unlock, the Windows Camera app showed no picture from this unit.
- Run as administrator, the tool printed detailed output, and the Camera app then showed an image.

**A locked Leap looks healthy.** It enumerates, accepts every control, returns valid calibration, and negotiates a stream. It just never sends data. To check a unit, open the Windows Camera app: a green and magenta image means it's unlocked.

The installer force-installs a driver, stops `LeapService`, runs `FirmwareReset`, and restarts the service. `RestoreDevice.exe` runs the same steps with a different archive.

### First frames on macOS, 2026-11-04

After the unlock, a plain `leapcap` capture delivered frames through AVFoundation with no root, no libusb and no replay.

| Measure | Result |
|---|---|
| Mode | 640×480, `yuvs` |
| Frames seen in 8 s | 450 |
| Measured rate | 56.3 FPS, against 57.5 advertised |
| Frames dropped by AVFoundation | 0 |
| Driver packets received | 17447, none dropped |
| Frame size | 614,400 bytes |

The unlock changed nothing visible over USB: IDs, strings and the 0.00 version all stayed the same. Calibration matches its backup exactly. So only streaming behaviour tells a locked unit from an unlocked one.

Addresses 0x00 to 0x0D hold ASCII text, `LP10227151906` on this unit. It looks like a serial number, and stayed the same before and after the unlock. Since USB reports no serial, this is a candidate key for the per-device calibration cache in Phase 1.

### Phase 0 result: Path A, 2026-11-04

All four probe checks passed at commit `4956164`, with no root and no replay. **Frames come through AVFoundation, controls go over libusb on endpoint 0, and calibration comes from the device.**

| Check | Evidence |
|---|---|
| Device visible | Listed with `yuvs` at all six modes |
| Bytes intact | Stereo correlation 0.89, values 0 to 255, metadata exact |
| Controls work | Exposure, gain and LEDs all changed the stream while it ran |
| Calibration valid | Repeat reads match, and match the backup |

The 30-second run measured 57.55 FPS over 1727 frames, with none dropped. The first frame arrived 0.2 s after capture started.

**Control behaviour measured:**

- **Metadata wraps as expected.** Exposures of 2000 and 8000 read back as 464 and 320, and gain 40 reads back as 8. These are exact, and they sit in the frame bytes, so the data arrives unaltered.
- **Both gain forms work.** The plain and `0x4000` selector forms gave near-identical images. It isn't known whether the device treats them differently in any way, so Phase 1 keeps the repo's plain form.
- **LEDs are controllable.** Mean brightness fell from 149 to 4.4 with them off, and returned to 149.
- **Orientation:** with the long edge horizontal and the green light at the bottom, the image is upright. Earlier sideways images came from how the Leap was resting, and there's no sign of a sensor rotation. The rigid mount should keep this orientation.

**Open items to carry forward:**

- **Exposure response is weak.** Four times the exposure raised mean brightness only 22 to 30%, below the probe's own 1.3× threshold in the final run. Check 3 passed on other controls. Gamma, saturation and LED output may all contribute, none tested. Exposure shouldn't be treated as linear.
- **Frame alternation: settled in Phase 1.** Logging 3454 consecutive frames found `frame_label` was 1 on every one, so it doesn't alternate in this mode, and the earlier result wasn't aliasing.
- **Left and right brightness can differ.** Left read about 10% dimmer in the final run but equal in the one before, which suggests the scene rather than a sensor offset. It needs checking.
- **The 13-second delay is only explained by elimination.** It disappeared once OpenCV was imported before streaming, but its cause wasn't measured directly.
- **Restored settings weren't verified.** The probe restores exposure and gain, but it doesn't read them back afterwards.
- **No IR baseline exists for the locked state,** so we don't know whether the replay was what lit the LEDs. It no longer affects this project.
- **Unlock survival is untested.** Nobody has checked whether later Leap or Ultraleap software would undo it.

### Phase 1 soak test, 2026-11-04

A 10-minute stream through the full capture path, at commit `847e3f9`, on a still scene with fixed controls:

| Measure | Result |
|---|---|
| Frames received | 34,532 in 600 s |
| Measured rate | 57.55 FPS, steady throughout |
| Skipped, torn, dropped | 0, 0, 0 |
| Largest frame gap | 17.8 ms |
| Startup from cache | 0.37 s, against 1.74 s with a device read |

Every published frame is accounted for: 1 before the reader started, 34,532 received, and 5 after it stopped.

**Brightness settles.** Mean brightness fell about 1.9% over the soak and was flat for the last three minutes. Part of the fall repeats at each stream start, so brightness-sensitive code should skip the first second or so. The cause isn't known.

**Not covered:** stream stability while controls change, or under a heavy stereo workload in the same process. Phase 3 will exercise the second one.

### Black image in cameraControlExample

The example showed black from the first frame on macOS, while `minimumExample` worked. **OpenCV 5.0 calls a slider's callback when the slider is created,** so the example's `Dgtl Gain` slider, which starts at 0, sent digital gain 0 at launch. Digital gain 0 gives a black image.

The macOS library now clamps digital gain to the device's range of 4 to 15. An explicit 0 or 16 becomes the nearest valid value, because both give black. Whether older OpenCV versions on Windows behave the same is untested.

**Control measurements, with one reading per setting on one scene:**

| Control | Finding |
|---|---|
| Power-on values | Exposure 300, gain 16, digital gain 4, gamma on, no dark frames |
| Persistence | Settings survive across streams and processes until a replug, so they're now reset before every stream |
| Digital gain | Device range 4 to 15. Mean brightness 19 at 1, 76 at 4, 135 at 8, and 0 at both 0 and 16 |
| Analog gain | Device range 16 to 63. Values below 16 image like 16. Metadata reports the raw value, wrapped at 32 |
| Exposure | Mean rises only from 77 to 143 between 300 and 32222 µs, with gamma on. Long exposures lower the frame rate: about 32 ms gave 31 FPS |
| Gamma | Off gave a mean of 59, on gave 77 |
| HDR | On gave 49 against 77, and left and right brightness matched |

**Rectification works here.** Maps are sensible, with the centre pixel moving only a few pixels and 97% of output covered. Rectified images are recognisable and straightened. The black result in upstream issue #17 didn't reproduce, though metric accuracy is still untested until Phase 2.

### Phase 1 result, 2026-11-04

Phase 1 is complete at commit `1b71a2d`. All 88 unit tests pass, and both exit checks are met:

- **`minimumExample` and `cameraControlExample` run unedited.** Images are clear from the first frame, and every slider works. Rectify stayed clear, and quitting left no helper running.
- **The 10-minute soak was clean,** before the control fixes. A 10-second run after them was also clean, at 57.55 FPS with settings correctly reset.

**Open items to carry forward:**

- **The first frame can report an old exposure.** One frame after a reset read 20 before settling at 300. Anything using per-frame exposure should skip the first frame.
- **The earlier black image isn't fully closed.** That run ended at digital gain 7, which should show a picture, and we don't know whether it recovered at the time. The fixed run never went black, which argues against a second cause without proving it.
- **The soak predates the control fixes.** They only touch startup and value limits, but the 10-minute run wasn't repeated.
- **`stereoExample` hasn't been run on macOS.** Phase 2 replaces its role.

## Sources

- [OpenCV AVFoundation backend](https://github.com/opencv/opencv/blob/master/modules/videoio/src/cap_avfoundation_mac.mm)
- [macuvc / uvc-avf](https://github.com/VimalMollyn/uvc-avf)
- [libusb darwin backend](https://github.com/libusb/libusb/blob/master/libusb/os/darwin_usb.c)
- [CameraController](https://github.com/itaybre/CameraController)
- [purethermal1-uvc-capture issue #56](https://github.com/groupgets/purethermal1-uvc-capture/issues/56)
- [OpenMotion](https://github.com/coleman-sagil/OpenMotion)
- [wtfuzz/leapuvc](https://github.com/wtfuzz/leapuvc)

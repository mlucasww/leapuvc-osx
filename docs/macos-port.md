# macOS port: design notes and results

This records why the port is built the way it is and what testing found. Setup and run instructions are in [macos/README.md](../macos/README.md).

## Restarting this work

**Where it stands:** Phases 0 to 3 are complete. The Leap streams natively on Apple Silicon at 57.55 FPS with no root, the Python library and examples work, corrected depth is within 1.5% from 0.25 to 2 m on the test unit, and `tools/live_cloud.py` shows a live point cloud in Rerun at 30 clouds a second. One known issue is left open on purpose: the test Leap slows to 45–53 FPS about 90 minutes after it's plugged in (see [The Leap slows down after being plugged in](#the-leap-slows-down-after-being-plugged-in)). Phase 4, the camera mount, is next.

**Ground rules, which still apply:**

- Don't compile or run code on the development MacBook. Write code there, then build and run on the Sequoia test Mac.
- Start hardware tests from Terminal.app on the test Mac, because camera permission follows the launching app.
- The test Mac keeps the repo at `/Users/wildwood/dev/leapuvc-osx`, on macOS 15.8.1. Its Claude session reaches this one through Remote Control.
- Don't rerun `tools/bringup_test.py`. Don't install Leap or Ultraleap software on anything this unit connects to, because nobody has tested whether it would undo the unlock.
- If a Leap sends no frames, confirm the unlock first: `UnlockDevice.exe` must run as administrator, and the Windows Camera app should show an image.

**To restart:**

1. On the test Mac, from the `macos/` folder, rebuild. Setup now uses Homebrew's Python 3.12, which Rerun needs:

   ```bash
   make setup && make leapcap && make test
   ```

   Expect 134 tests to pass.

2. Run a quick health check, which should report a clean run at about 57.55 FPS:

   ```bash
   .venv/bin/python tools/stream_test.py --seconds 10
   ```

   A run below 99% of the mode's rated rate isn't clean. If the Leap has been plugged in for more than an hour or so, replug it first.

3. Open the live point cloud, with the test unit's offset:

   ```bash
   .venv/bin/python tools/live_cloud.py --disparity-offset 0.168
   ```

**Next: Phase 4, camera mount.** Mount the Leap rigidly on the mirrorless camera, and work out how the Leap's coordinates relate to the camera's view.

**Left over from Phase 3,** none of them needed for scene layout so far:

- PLY snapshots of the cloud.
- Averaging over frames, or regions, to steady depth beyond about 1.5 m.
- Exposure chosen by distance: close targets saturate at high gain, and far ones need analog gain.
- The cause of the Leap's slowdown. Replugging before a long run works around it.

**Keep in mind:**

- Mount the Leap with its long edge horizontal and the green light at the bottom.
- Depth needs a textured scene. Plain walls and glossy surfaces give no depth.
- Skip the first second of brightness data and the first frame's exposure metadata.
- An ordinary LED lamp adds no infrared light. Only the Leap's own LEDs light the scene.
- If the viewer stops showing new clouds, quit it and let the next run open a new one.

**Later phases:** Phase 4 adds the camera mount, and Phase 5 finishes the documentation. Tethered camera control stays out of scope for now.

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
| 2. Metric stereo | Done: corrected depth within 1.5% from 0.25 to 2 m |
| 3. Live point cloud | Done: 30 clouds a second live, nothing skipped. The Leap's slowdown is a known issue |
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

### Phase 2 first measurements, 2026-11-05

The Leap faced down at a table, with an 8.5 × 11 in sheet of paper as the target. Distances are from the Leap's front face, at commit `18d861c`, default exposure and gain:

| True distance | SGBM depth | Error | Correlation disparity | Expected disparity | Row offset |
|---|---|---|---|---|---|
| 0.592 m | 0.662 m | +11.9% | 8.30 px | 9.02 px | 0.8 px |
| 0.837 m | 0.939 m | +12.3% | 5.60 px | 6.38 px | 0.6 px |

A first run at 0.592 m read 0.780 m, but a glossy LED reflection in the centre pulled its median, so it's excluded.

**What the evidence says:**

- **Focal length is right.** Sizing the paper gives distances within about 2%.
- **Disparity is short by 0.7 to 1 px.** This could be a scale error of about 0.89, equivalent to a 35.6 mm baseline, or a constant offset. Two distances can't tell them apart.
- **Rows are off by 0.6 to 0.8 px,** so rectification isn't fully aligned.
- **The alignment check returned nothing:** frames had a mean brightness of about 10 out of 255, too dark for feature matching.
- **The stream ran 57.35 FPS** in a 10-second check, with one 52 ms gap.

**Leading suspect: the rotation convention.** The factory rotations differ between the cameras by about 0.006 rad about the vertical axis. At 134 px focal length that's 0.8 px of disparity, the size of the error. If the sign is wrong, or the Cayley transform's factor of 2 is, rectification would leave this error behind. The blob's per-camera `center` values are unused, but they differ by 5.7 px in x, far more than the error, so they're a weaker suspect.

**Next:** `depth_test.py` now saves raw frames, measures disparity and row offset by correlation, and takes exposure and gain settings for dark scenes. `tools/rectify_variants.py` then scores each geometry reading against the tape measure offline. A run near 1.5 m would also separate scale from offset, with a predicted 1.68 m for scale or 1.90 m for offset.

A setup diagram from that session is at https://claude.ai/artifact/936KBzGDR5MTefsq35rgdL.

### Wall measurements, 2026-11-07

The Leap was on a tripod, mounted horizontally with the green light at the bottom. It faced a crumpled-paper patch about 95 px across taped to a wall, and distances came from a laser tape. Settings were 4000 µs exposure, with digital gain 4 to 15 to keep brightness up. Instructions are at https://claude.ai/artifact/TzfGTVvLNQ8nxFJk5oRU7c.

These use factory rectification and SGBM. Medians step in 1/16 px.

| True | Measured | Error | Disparity | Expected | Shortfall |
|---|---|---|---|---|---|
| 0.255 m | 0.267 m | +4.7% | 20.00 px | 20.94 px | 0.95 px |
| 0.497 m | 0.534 m | +7.5% | 10.00 px | 10.75 px | 0.75 px |
| 1.103 m | 1.285 m | +16.5% | 4.16 px | 4.84 px | 0.69 px |
| 1.536 m | 1.709 to 1.744 m | +11 to +14% | 3.06 to 3.13 px | 3.48 px | 0.35 to 0.41 px |
| 2.027 m | 2.035 m | +0.4% | 2.63 px | 2.63 px | 0.01 px |

**The shortfall is a roughly constant offset, not a scale error.** It stays at 0.7 to 0.9 px up to 1.1 m. A 0.89 scale error would mean a 2.3 px shortfall at 0.255 m. Past 1.5 m the runs are too noisy to judge: frame-to-frame spread was 0.14 to 0.6 m, and frames were dark even at maximum digital gain. Analog gain and longer exposure weren't tried.

**Leaving the rotation out looked best on the one reliable run,** at 0.497 m:

| Geometry | Error | Row offset |
|---|---|---|
| Factory rotation | +10.6% | 0.44 px |
| Transposed | −5.7% | −0.22 px |
| No rotation | +0.9% | 0.0 px |
| With `center` offsets | over +500% | — |

The `center` offsets are clearly wrong as a reading. The rest of the variant scores came from a correlation window too small for the patch and for near disparities, so they were noise. The tool now scores each variant with SGBM, and fits scale and offset across distances.

### Decision: leave out the factory rotation

Rescoring the six kept wall runs with SGBM, under each geometry reading, gave this. Scale and offset fit measured against expected disparity. Rows are the median row offset.

| Geometry | Scale | Offset | Median error, near runs | Rows |
|---|---|---|---|---|
| **No rotation** | 0.99 | +0.26 px | **1.9%** | **0.10 px** |
| Factory rotation | 0.98 | −0.55 px | 7.9% | 0.65 px |
| Transposed | 1.00 | +1.01 px | 8.7% | 0.43 px |
| With `center` offsets | 0.99 | +5 to +7 px | over 30% | 0.4 to 0.65 px |

Near runs are 0.255, 0.497 and 1.103 m. Scale and offset are fitted on those three.

**`StereoRig` now leaves the factory rotation out by default.** Every reading has a scale near 1, so the error was always an offset. Factory and transposed land almost symmetrically either side of no rotation. That suggests the stored rotation is either already applied in the image, or uses a convention we can't reproduce. The `center` values add about 5 px whichever way they're applied, so they aren't meant for this.

`parseLeapCalibration`, shared with Windows and Linux, still applies the factory rotation for its preview maps. That's unchanged, because it's untested on those platforms.

**Depth accuracy with no rotation, one unit, against a laser tape:**

| True distance | Leap reading | Error | Notes |
|---|---|---|---|
| 0.255 m | 0.256 m | −0.3% | |
| 0.497 m | 0.488 m | −1.9% | |
| 1.103 m | 1.060 m | −3.9% | |
| 1.536 m | 1.37 to 1.39 m | −9 to −11% | Under-lit, noisy |
| 2.027 m | 1.67 m | −18% | Under-lit, very noisy |

Up to about 1.1 m, depth is within 4%. That's good enough for judging scene layout. The remaining +0.26 px surplus would make readings slightly short and isn't corrected, because it comes from one unit and three runs.

Beyond 1.5 m, the frames were too dark: digital gain was at its maximum, and analog gain and longer exposure weren't tried. The 2 m error mostly reflects noise, so the far range still needs measuring with better exposure. The LEDs light only nearby objects, so expect usable depth to about 1.5 m indoors.

### Phase 2 result: corrected depth accuracy, 2026-11-07

Better-exposed far runs, with 8000 µs exposure and analog gain 40, showed the same small error as the near runs. With no rotation, disparity reads high by a constant amount at every distance. Fitting that offset alone, with scale fixed at 1, over the five well-exposed runs gives **+0.168 px, with a spread of 0.053 px**. A free fit gives scale 0.993 and offset +0.22 px, which agrees. Rows align to a median of 0.09 px.

| Laser | Uncorrected | Error | Corrected for 0.168 px | Error | Frame-to-frame spread |
|---|---|---|---|---|---|
| 0.255 m | 0.254 m | −0.3% | 0.256 m | +0.5% | 0.002 m |
| 0.497 m | 0.488 m | −1.9% | 0.495 m | −0.3% | under 0.001 m |
| 1.103 m | 1.060 m | −3.9% | 1.097 m | −0.6% | 0.04 m |
| 1.493 m | 1.425 m | −4.5% | 1.492 m | −0.1% | 0.08 m |
| 2.035 m | 1.887 m | −7.3% | 2.006 m | −1.4% | 0.40 m |

Corrected values use each run's averaged disparity from `rectify_variants.py`, so they differ slightly from depth_test's medians, which step in 1/16 px.

**Corrected depth is within 1.5% from 0.25 to 2 m.** Two cautions:

- **The offset was fitted on these same five runs,** so this table shows consistency, not an independent check. It's also one unit's value. Each Leap should fit its own with `tools/rectify_variants.py`, then pass it as `disparity_offset_px` or `--disparity-offset`.
- **Single frames get noisy with distance:** about ±4 cm at 1.1 m, ±8 cm at 1.5 m and ±40 cm at 2 m. Phase 3 should average over frames, or over regions, to use depth beyond about 1.5 m.

**Exposure depends on distance.** Close targets need low gain, or the LED hotspot saturates them, as at 0.255 m and 1.493 m. Far ones need analog gain. A single fixed setting won't suit both, so Phase 3 needs automatic exposure, or settings chosen per scene.

**Other notes:**

- A centred LED hotspot can saturate a close target, as at 0.255 m with gain 8. Lower the gain until the target shows texture.
- An ordinary LED lamp adds no infrared light, so it doesn't help.

## Phase 3: live point cloud

`leapmac/pointcloud.py` builds a cloud from each frame: rectify with `StereoRig`, match with SGBM, convert to depth after the unit's offset, keep 0.1 to 2.5 m, and keep every second pixel each way. `tools/live_cloud.py` runs it on the newest frame and logs the cloud, the rectified left image and the depth image to Rerun 0.42. Capture runs on its own thread and still counts every frame, so the report can say whether stereo work cost any.

**Display rate is capped at 30 clouds a second.** Each cloud sends the viewer about 0.8 MB, so 30 a second is about 25 MB/s. The viewer is given 2 GB and drops the oldest data past that, which keeps about 80 seconds.

### Replay benchmark, 2026-11-07

Frames saved by `depth_test.py` at 0.497 m, replayed for 10 s on the test Mac, written to a file rather than a viewer:

| Replay | Clouds per second | Processing, median | 95th percentile | Logging, median | Points per cloud |
|---|---|---|---|---|---|
| As fast as possible, every frame | 59.8 | 14.5 ms | 17.1 ms | 1.7 ms | about 38,000 |
| 57.5 FPS, every frame | 57.5 | 14.4 ms | 16.9 ms | 1.8 ms | about 38,000 |
| 57.5 FPS, capped at 30 | 30.0 | 14.3 ms | 15.3 ms | 1.9 ms | about 38,000 |

SGBM is most of the processing time. So the pipeline can keep up with every frame, though with little headroom, which is one more reason for the cap.

### Hardware checks, 2026-11-07 and 08

**The pipeline keeps up live.** The second 10-minute soak, with the viewer open and SGBM on all cores:

| Measure | Result |
|---|---|
| Clouds per second | 29.96, against the cap of 30 |
| Processing, median and 95th percentile | 15.2 ms and 17.0 ms |
| Logging, median | 2.1 ms |
| Points per cloud, median | about 26,000 |
| Frames skipped by the reader, torn or dropped | 0, 0 and 0 |
| Capture rate | 48.2 FPS, because the Leap had slowed down (below) |

The first soak, on 2026-11-07, skipped 6 frames with a 168 ms gap. That came from opening the viewer, not from the pipeline (see [Viewer](#viewer)). The second soak reused an open viewer and skipped none.

**Depth at measured distances.** A target at 0.5 m reads correctly in the viewer. A target at 1.0 m appears only as a rough cloud, which is expected for this short-range camera:

- An object at 1 m is only about 5.3 px apart in the two images, against 10.7 px at 0.5 m.
- Depth error grows with the square of distance, so a quarter-pixel matching error is about ±1.2 cm at 0.5 m but about ±5 cm at 1 m. Single-frame points spread along the line of sight.
- The lens is very wide, so a 20 cm object at 1 m covers about 27 px, or 13 points across at the default stride of 2.

Phase 2's "within 1.5% to 2 m" still holds, because it took a median over a region and over several frames. Averaging disparity over a few frames, for a still scene, would be the cheapest improvement if the far range matters.

### Viewer

**A fresh viewer updates live** at 30 clouds a second. One viewer left open overnight, through several runs, stopped showing new clouds while the tool kept sending them. Quitting it and letting the next run open a new one fixed that. The likeliest cause is that the timeline stopped following the newest data after a click in it, which Rerun allows.

**Opening a new viewer used to skip frames.** Every run that started a new viewer skipped 6 to 10 frames, with a gap of about 150 ms. Runs that reused an open one skipped none. The tool started capture before the viewer, and while the viewer started up nothing read the ring, whose four slots last about 70 ms. The viewer now opens before capture starts. The first run after the fix opened a new viewer and skipped nothing, with a largest gap of 19.6 ms, where the two runs just before it each skipped 7.

### The Leap slows down after being plugged in

On 2026-11-07 the capture rate wandered between 42 and 55 FPS from run to run, where Phases 0 and 1 had measured a steady 57.55. Every run was still clean in every other way: no frames skipped, torn or dropped. The frame interval itself stretched, from 17 ms to 18 ms in mild cases and 21–25 ms in the worst. A replug brought back 57.5 FPS each time, until the next slowdown.

**What it isn't:**

| Suspect | Evidence against it |
|---|---|
| Phase 3's load | Capture alone was slowest of all, at 42.5 FPS. The first live run was already slow before any viewer was open |
| Exposure or gain | Every frame's embedded line reads exposure 300 and gain 16, in healthy and slow runs alike |
| Heat from streaming | The second soak was slow from its first minute |
| Power or a weak link | Mains power, Low Power Mode off, connected straight to a root port at 480 Mb/s |
| One USB port or controller | Moving to the other USB-C port, on its own controller, gave 57.53 FPS at first, then about 50 FPS some 90 minutes later |
| macOS's camera driver alone | Restarting UVCAssistant without replugging raised the rate from 45.1 to only 53.1 FPS, within the run-to-run variation |

**It's a throughput limit.** The slow runs move about the same bytes per second whatever the frame size:

| Run | Frame rate | Throughput |
|---|---|---|
| Healthy, 640×480 | 57.55 FPS | 35.4 MB/s |
| Slow, 640×480 | 45.1 FPS | 27.7 MB/s |
| Slow, 640×240, rated 115 FPS | 98.7 FPS | 30.3 MB/s |

640×480 at 57.5 FPS needs 35.4 MB/s over a bulk endpoint, close to what USB 2.0 carries in practice. When less gets through, the Leap waits rather than dropping frames.

**Timing:** twice now, a replug gave a healthy stream and about 90 minutes later the Leap was slow again: 21:46 healthy and 23:15 slow on the first port, 10:45 healthy and 12:14 slow on the second. The Leap had been idle in between both times. The likeliest cause is in the Leap itself, perhaps its temperature while powered, but two cases don't prove that. On 2026-11-04 it stayed healthy from the first probe at 21:56 to the last stream at 23:15, and again the next morning, so the timing isn't exact.

**Tool changes that came out of this:**

- `stream_test.py` and `live_cloud.py` only call a run clean if the capture rate is close to the rated one: 99% of it for `stream_test.py` and 57 FPS for `live_cloud.py`. Before, a steady 42 FPS run passed.
- `live_cloud.py` records the capture rate for each minute in its report, so a slowdown shows when it started.
- `leapcap` ignores Ctrl-C and stops when Python tells it to, so a run stopped early still reports its frame counts. Before, Ctrl-C reached the helper too and the report lost them.

## Sources

- [OpenCV AVFoundation backend](https://github.com/opencv/opencv/blob/master/modules/videoio/src/cap_avfoundation_mac.mm)
- [macuvc / uvc-avf](https://github.com/VimalMollyn/uvc-avf)
- [libusb darwin backend](https://github.com/libusb/libusb/blob/master/libusb/os/darwin_usb.c)
- [CameraController](https://github.com/itaybre/CameraController)
- [purethermal1-uvc-capture issue #56](https://github.com/groupgets/purethermal1-uvc-capture/issues/56)
- [OpenMotion](https://github.com/coleman-sagil/OpenMotion)
- [wtfuzz/leapuvc](https://github.com/wtfuzz/leapuvc)

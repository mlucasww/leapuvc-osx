# LeapUVC on macOS

This folder is the Apple Silicon port of LeapUVC. The goal is a live 3D point cloud from an unlocked Leap Motion Controller, to help judge scene layout for a mirrorless camera.

**Status:** On an unlocked Leap, the capture library streams at 57.55 FPS without root, and metric depth is within 4% up to 1.1 m. Phase 3, the live point cloud, is written and waiting for its hardware checks.

Design notes and results are in [docs/macos-port.md](../docs/macos-port.md).

## What you need

- A Mac running macOS Sequoia on Apple Silicon
- An unlocked Leap Motion Controller, plugged in directly rather than through a hub. On Windows, right-click `LeapUVC Firmware Tools/UnlockDevice.exe` and choose **Run as administrator**. Without that, it can exit quietly and change nothing. The Windows Camera app then confirms the unlock: it should show an image, usually green and magenta. A locked Leap still accepts controls and returns calibration, but it never sends video.
- Xcode Command Line Tools, Homebrew and Python 3.11 or later. The Makefile uses Homebrew's `python3.12`, because macOS's own `python3` is 3.9

Quit any other software that might use the camera, including Leap or Ultraleap services.

## Setup

Run these from this `macos/` folder:

```bash
xcode-select --install
```

```bash
brew install libusb python@3.12
```

```bash
make setup
```

```bash
make leapcap
```

## Check the code without a Leap

```bash
make test
```

This covers frame decoding, descriptor parsing, calibration parsing and the Phase 0 decision logic.

## Run the hardware probe

Run this from **Terminal.app**, not from a remote shell or an app's built-in terminal:

```bash
make probe
```

macOS ties camera permission to the app that launches the probe. Terminal.app will ask for it the first time. Other apps may be refused silently, which shows up as a `blocked` result.

Point the Leap at a detailed scene about 0.5 to 1 m away. The LEDs will switch off briefly while it tests them. The probe takes about 30 seconds.

Results go to `probe_out/<timestamp>/`:

| File | Contents |
|---|---|
| `report.md` | Summary and recommended capture path |
| `report.json` | Full evidence for every check |
| `calibration.bin` | The device's 156-byte factory calibration |
| `samples/` | Sample frames, raw and as left/right PNGs |

### Reading the result

| Status | Meaning |
|---|---|
| `pass` | This part of the path works |
| `fail` | It does not work, so the planned fallback applies |
| `inconclusive` | Someone needs to look at the saved evidence |
| `blocked` | Something else stopped the test. Fix it and run again |
| `skipped` | An earlier problem meant this check was not reached |

If the Leap isn't found, the probe lists every USB device in `report.json`. Run it again with the right IDs:

```bash
.venv/bin/python tools/probe_leap.py --vid 0xf182 --pid 0x0003
```

## Use the Leap from Python

`leapmac.capture.leapImageThread` has the same API as `Python/leapuvc.py`, with capture, controls and calibration running natively on macOS. Your first run reads calibration from the device. After that it comes from a per-device cache in `~/Library/Caches/leapmac`.

Run the repo's unchanged examples from Terminal.app, and press `q` in a video window to quit:

```bash
.venv/bin/python tools/run_example.py minimumExample
```

```bash
.venv/bin/python tools/run_example.py cameraControlExample
```

To check the stream, count every frame for 10 minutes:

```bash
.venv/bin/python tools/stream_test.py --seconds 600
```

Mount the Leap with its long edge horizontal and the green light at the bottom, which gives an upright image.

## Show the live point cloud

Run this from Terminal.app:

```bash
make cloud
```

The Rerun viewer opens with the 3D cloud, the rectified left image, the depth image, and plots of rates and processing time. Press Ctrl-C in Terminal to stop. Each run writes `probe_out/cloud-<timestamp>.json`, and `capture_clean` in it says whether capture lost anything while stereo ran.

The tool takes the same exposure and gain options as `depth_test.py`, plus these:

| Option | Default | Effect |
|---|---|---|
| `--disparity-offset` | 0 | This unit's fitted offset in pixels. The test unit measured about 0.17 |
| `--rate` | 30 | Most clouds per second. Capture keeps every frame either way, and 0 processes every frame |
| `--min-depth`, `--max-depth` | 0.1, 2.5 | Depth range kept, in metres |
| `--stride` | 2 | Keep every nth pixel in each direction |
| `--seconds` | 0 | Stop after this long. 0 runs until Ctrl-C |
| `--save` | none | Write an `.rrd` file instead of opening the viewer |

To try it without a Leap, replay frames saved by `depth_test.py`:

```bash
.venv/bin/python tools/live_cloud.py --replay probe_out/depth-20261107-155252/raw.npz
```

Rerun collects anonymous usage data from its viewer. To turn that off, run `.venv/bin/rerun analytics disable`.

## Measure depth accuracy

Place a flat, textured target square-on and centred, then measure from the Leap's front face to the target. Run this from Terminal.app, at each distance, in metres:

```bash
.venv/bin/python tools/depth_test.py --distance 1.0 --preview
```

Press space to measure, or `q` to cancel. To print the error table:

```bash
.venv/bin/python tools/depth_test.py --summary
```

## Test a bring-up sequence

You shouldn't need this. It was written to find out whether a recorded sequence of control writes could wake a silent Leap, and all three scenarios failed on our test unit. The real cause was that the unit hadn't been unlocked, so check that first, and don't rerun these without a specific reason. The sequence writes bytes to device registers that may be persistent, as the [notes on it](third_party/openmotion/README.md) explain.

Each scenario runs from Terminal.app, with no root:

```bash
.venv/bin/python tools/bringup_test.py before
```

| Scenario | What it does |
|---|---|
| `before` | Replays every row, then starts capture |
| `during` | Starts capture so reads are pending, then replays every row |
| `reset` | Replays up to the recorded reset, resets the device, replays the rest, then captures |

Results go to `probe_out/bringup-<scenario>-<timestamp>/`. The sequence comes from [OpenMotion](third_party/openmotion/README.md).

## Layout

| Path | Purpose |
|---|---|
| `leapcap/leapcap.swift` | AVFoundation capture helper |
| `leapmac/capture.py` | The `leapImageThread` API on macOS |
| `leapmac/ring.py` | Reads frames from the shared-memory ring |
| `leapmac/helper.py` | Runs the capture helper and follows its events |
| `leapmac/controls.py` | Runs every USB control on one thread |
| `leapmac/calibration_cache.py` | Per-device calibration cache |
| `leapmac/devices.py` | Finds the Leap among AVFoundation's cameras |
| `leapmac/stereo.py` | Shared rectification, disparity and metric depth |
| `leapmac/pointcloud.py` | Builds a point cloud from each stereo frame |
| `leapmac/frames.py` | Frame decoding and image statistics |
| `leapmac/uvc_descriptors.py` | USB descriptor parsing |
| `leapmac/uvc_control.py` | UVC controls through libusb |
| `leapmac/calibration_blob.py` | Calibration blob parsing |
| `leapmac/probe_decision.py` | Chooses a capture path from the check results |
| `leapmac/bringup.py` | Parses and replays recorded control writes |
| `tools/probe_leap.py` | The hardware probe |
| `tools/run_example.py` | Runs the repo's Python examples unchanged on macOS |
| `tools/stream_test.py` | Soak test that counts every frame |
| `tools/live_cloud.py` | Live point cloud in the Rerun viewer |
| `tools/depth_test.py` | Tape-measure test of depth accuracy |
| `tools/rectify_variants.py` | Scores alternative geometry readings against saved runs |
| `tools/bringup_test.py` | Tests whether a bring-up wakes the video stream |
| `tools/read_registers.py` | Reads known-safe registers and checks calibration against a backup |
| `third_party/openmotion/` | The recorded sequence and its licence |

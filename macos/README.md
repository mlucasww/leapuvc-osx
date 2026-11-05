# LeapUVC on macOS

This folder is the Apple Silicon port of LeapUVC. The goal is a live 3D point cloud from an unlocked Leap Motion Controller, to help judge scene layout for a mirrorless camera.

**Status:** Phase 0. Controls and calibration work without root, but the Leap hasn't sent any video on macOS, even after a bring-up replay. The next step is to confirm whether this unit streams at all on Windows or Linux.

Design notes and results are in [docs/macos-port.md](../docs/macos-port.md).

## What you need

- A Mac running macOS Sequoia on Apple Silicon
- An unlocked Leap Motion Controller, plugged in directly rather than through a hub. Unlocking needs the Windows tools in this repo.
- Xcode Command Line Tools, Homebrew and Python 3.11 or later

Quit any other software that might use the camera, including Leap or Ultraleap services.

## Setup

Run these from this `macos/` folder:

```bash
xcode-select --install
```

```bash
brew install libusb
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

## Test a bring-up sequence

The first probe found that the Leap accepts controls but sends no video. This test replays a recorded sequence of control writes to find out whether that wakes it. All three scenarios have already failed on our test unit, so don't rerun them without a specific reason. The sequence writes bytes to device registers that may be persistent, as the [notes on it](third_party/openmotion/README.md) explain.

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
| `leapmac/frames.py` | Frame decoding and image statistics |
| `leapmac/uvc_descriptors.py` | USB descriptor parsing |
| `leapmac/uvc_control.py` | UVC controls through libusb |
| `leapmac/calibration_blob.py` | Calibration blob parsing |
| `leapmac/probe_decision.py` | Chooses a capture path from the check results |
| `leapmac/bringup.py` | Parses and replays recorded control writes |
| `tools/probe_leap.py` | The hardware probe |
| `tools/bringup_test.py` | Tests whether a bring-up wakes the video stream |
| `tools/read_registers.py` | Reads known-safe registers and checks calibration against a backup |
| `third_party/openmotion/` | The recorded sequence and its licence |

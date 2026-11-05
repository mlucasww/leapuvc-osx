#!/usr/bin/env python3
"""Run one of the repo's Python examples on macOS, without editing it.

The examples `import leapuvc`. This points that import at the macOS version,
then runs the example as usual. Run it from Terminal.app, from the macos/
folder, and press q in a video window to quit:

    .venv/bin/python tools/run_example.py minimumExample
    .venv/bin/python tools/run_example.py cameraControlExample
    .venv/bin/python tools/run_example.py stereoExample
"""

import argparse
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("example", help="the example's name, such as minimumExample")
    args = parser.parse_args()

    path = ROOT.parent / "Python" / f"{args.example}.py"
    if not path.exists():
        sys.exit(f"No example at {path}")

    from leapmac import capture

    sys.modules["leapuvc"] = capture
    runpy.run_path(str(path), run_name="__main__")


if __name__ == "__main__":
    main()

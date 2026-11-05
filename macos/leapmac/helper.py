"""Run `leapcap stream` and follow the JSON events it prints."""

import json
import subprocess
import threading
import time

STOP_TIMEOUT_S = 5.0

# Eight slots give a reader about 100 ms of headroom at 57.5 FPS, in order.
RING_SLOTS = 8


class StreamHelper:
    def __init__(self, leapcap, unique_id, width, height, ring_path, pixel_format="yuvs"):
        self.command = [
            str(leapcap), "stream",
            "--unique-id", unique_id,
            "--ring", str(ring_path),
            "--width", str(width),
            "--height", str(height),
            "--pixel-format", pixel_format,
            "--slots", str(RING_SLOTS),
        ]
        self.events = []
        self.ended = False
        self.process = None
        self._condition = threading.Condition()

    def start(self):
        self.process = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._reader = threading.Thread(target=self._read, daemon=True, name="leapcap-events")
        self._reader.start()

    def _read(self):
        for line in self.process.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            with self._condition:
                self.events.append(event)
                self._condition.notify_all()
        with self._condition:
            self.ended = True
            self._condition.notify_all()

    def event(self, kind):
        with self._condition:
            return next((event for event in self.events if event.get("event") == kind), None)

    def wait_for(self, kind, timeout):
        """Return the first event of this kind, or an error event, or None."""
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                for event in self.events:
                    if event.get("event") in (kind, "error"):
                        return event
                remaining = deadline - time.monotonic()
                if self.ended or remaining <= 0:
                    return None
                self._condition.wait(remaining)

    def alive(self):
        return self.process is not None and self.process.poll() is None

    def stop(self):
        """Ask the helper to stop, so it reports its frame counts."""
        if self.process is None:
            return
        try:
            self.process.stdin.write("stop\n")
            self.process.stdin.close()
        except (BrokenPipeError, ValueError):
            pass
        try:
            self.process.wait(timeout=STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=STOP_TIMEOUT_S)
        self._reader.join(timeout=STOP_TIMEOUT_S)

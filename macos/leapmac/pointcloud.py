"""Turn each Leap stereo frame into a point cloud for the live view.

CloudBuilder runs the whole per-frame pipeline: rectify, match with SGBM,
convert disparity to depth, and keep the points in a useful depth range. It
holds no thread of its own, so the caller decides which frames to process. The
live tool processes the newest frame and lets the capture thread count the rest.

Points are in the left rectified camera's frame: x right, y down, z forward, in
metres.
"""

import time
from collections import deque
from dataclasses import dataclass, field

import numpy as np

from . import stereo

# Depth is noisy past about 1.5 m indoors: at 2.5 m a quarter-pixel matching
# error is already about 0.3 m. The near limit is just inside SGBM's reach.
DEFAULT_MIN_DEPTH_M = 0.1
DEFAULT_MAX_DEPTH_M = 2.5


@dataclass
class CloudSettings:
    min_depth_m: float = DEFAULT_MIN_DEPTH_M
    max_depth_m: float = DEFAULT_MAX_DEPTH_M
    # Keep every nth pixel in each direction. 2 gives at most 76,800 points.
    stride: int = 2
    num_disparities: int = stereo.DEFAULT_NUM_DISPARITIES
    block_size: int = stereo.DEFAULT_BLOCK_SIZE

    def __post_init__(self):
        if self.stride < 1:
            raise ValueError("stride must be at least 1")
        if not 0 < self.min_depth_m < self.max_depth_m:
            raise ValueError("depth range must satisfy 0 < min < max")


@dataclass
class Cloud:
    points: np.ndarray  # N x 3 float32, metres
    brightness: np.ndarray  # N uint8, from the rectified left image
    left: np.ndarray  # rectified left image
    depth: np.ndarray  # H x W float32 metres, NaN where nothing was kept
    timings_ms: dict = field(default_factory=dict)

    @property
    def size(self):
        return len(self.points)


class CloudBuilder:
    def __init__(self, rig, settings=None):
        self.rig = rig
        self.settings = settings or CloudSettings()
        self.matcher = stereo.make_matcher(self.settings.num_disparities, self.settings.block_size)

        # Each pixel's ray, x/z and y/z, computed once rather than per frame.
        rows, columns = np.indices((rig.height, rig.width), dtype=np.float32)
        self._ray_x = (columns - rig.cx) / rig.fx
        self._ray_y = (rows - rig.cy) / rig.fy
        stride = self.settings.stride
        self._sample = (slice(None, None, stride), slice(None, None, stride))

    def process(self, frame):
        """Build a cloud from a 2 x H x W left/right frame."""
        started = time.perf_counter()
        left, right = self.rig.rectify(frame[0], frame[1])
        rectified = time.perf_counter()
        disparity = stereo.compute_disparity(self.matcher, left, right)
        matched = time.perf_counter()

        with np.errstate(divide="ignore", invalid="ignore"):
            depth = self.rig.depth(disparity).astype(np.float32)
        # A disparity at or below the unit's offset gives infinite or negative
        # depth. The range check drops it, along with NaN.
        keep = (depth >= self.settings.min_depth_m) & (depth <= self.settings.max_depth_m)
        depth[~keep] = np.nan

        z = depth[self._sample]
        kept = np.isfinite(z)
        z = z[kept]
        points = np.empty((z.size, 3), dtype=np.float32)
        points[:, 0] = self._ray_x[self._sample][kept] * z
        points[:, 1] = self._ray_y[self._sample][kept] * z
        points[:, 2] = z
        brightness = left[self._sample][kept]
        finished = time.perf_counter()

        timings = {
            "rectify": (rectified - started) * 1000,
            "match": (matched - rectified) * 1000,
            "points": (finished - matched) * 1000,
            "total": (finished - started) * 1000,
        }
        return Cloud(points, brightness, left, depth, timings)


class RateMeter:
    """Count events over a sliding window of time, for a steady rate readout."""

    def __init__(self, window_s=2.0, clock=time.monotonic):
        self.window_s = window_s
        self.clock = clock
        self._times = deque()

    def tick(self):
        now = self.clock()
        self._times.append(now)
        cutoff = now - self.window_s
        while self._times and self._times[0] < cutoff:
            self._times.popleft()

    @property
    def rate(self):
        if len(self._times) < 2:
            return 0.0
        span = self._times[-1] - self._times[0]
        return (len(self._times) - 1) / span if span > 0 else 0.0

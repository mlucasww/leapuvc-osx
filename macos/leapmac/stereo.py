"""Turn a rectified Leap stereo pair into metric depth.

`parseLeapCalibration` rectifies each camera to its own camera matrix, which is
fine for a preview. Metric depth needs both images to share one matrix, so that
a point's disparity depends only on its distance. StereoRig builds those maps,
and by default leaves out the factory rotation, which measured worse.

Coordinates follow the left rectified camera: x right, y down, z forward, in
metres.
"""

from dataclasses import dataclass

import cv2
import numpy as np

DEFAULT_NUM_DISPARITIES = 96
DEFAULT_BLOCK_SIZE = 5
DISPARITY_SCALE = 16.0

# The baseline's factory unit is millimetres. Reject anything far from 40 mm.
BASELINE_RANGE_M = (0.03, 0.05)

MIN_ALIGNMENT_MATCHES = 10
MAX_MATCH_DISTANCE = 40


@dataclass
class StereoRig:
    width: int
    height: int
    fx: float
    fy: float
    cx: float
    cy: float
    baseline_m: float
    left_maps: tuple
    right_maps: tuple

    @classmethod
    def from_calibration(cls, calibration, resolution, rotation="identity", offsets=None):
        """Build shared rectification from a parseLeapCalibration result.

        `rotation` chooses how to use each camera's stored factory rotation:
        "identity" leaves it out, "factory" applies it, and "transposed"
        applies its inverse. Identity is the default because it measured best:
        on tape-measured wall runs it aligned rows to about 0.1 px, against
        0.65 px with the factory rotation, and cut the near-range depth error
        from about 8% to 2%. See docs/macos-port.md.

        `offsets` maps a camera name to a (dx, dy) shift, in pixels, of that
        camera's rectified image, for testing other readings of the geometry.
        """
        width, height = resolution
        baseline_m = float(calibration["baseline"]) / 1000.0
        if not BASELINE_RANGE_M[0] <= baseline_m <= BASELINE_RANGE_M[1]:
            raise ValueError(f"baseline of {baseline_m} m is outside the expected range")

        focals = [
            float(calibration[camera]["extrinsics"]["cameraMatrix"][0, 0])
            for camera in ("left", "right")
        ]
        fx = sum(focals) / 2.0
        fy = fx * height / 480.0
        cx, cy = width / 2.0, height / 2.0
        shared = np.array([[fx, 0, cx], [0, fy, cy], [0, 0, 1]], dtype=np.float64)

        offsets = offsets or {}
        maps = {}
        for camera in ("left", "right"):
            stored = np.asarray(calibration[camera]["extrinsics"]["r"], dtype=np.float64)
            rotations = {"factory": stored, "transposed": stored.T, "identity": np.eye(3)}
            if rotation not in rotations:
                raise ValueError(f"unknown rotation mode {rotation!r}")
            target = shared.copy()
            dx, dy = offsets.get(camera, (0.0, 0.0))
            target[0, 2] += dx
            target[1, 2] += dy
            maps[camera] = cv2.initUndistortRectifyMap(
                calibration[camera]["extrinsics"]["cameraMatrix"],
                calibration[camera]["intrinsics"]["distCoeffs"],
                rotations[rotation],
                target,
                (width, height),
                cv2.CV_16SC2,
            )
        return cls(width, height, fx, fy, cx, cy, baseline_m, maps["left"], maps["right"])

    @property
    def camera_matrix(self):
        return np.array([[self.fx, 0, self.cx], [0, self.fy, self.cy], [0, 0, 1]], dtype=np.float64)

    @property
    def Q(self):
        """The 4x4 matrix for cv2.reprojectImageTo3D, when fx equals fy."""
        return np.array(
            [
                [1, 0, 0, -self.cx],
                [0, 1, 0, -self.cy],
                [0, 0, 0, self.fx],
                [0, 0, 1 / self.baseline_m, 0],
            ],
            dtype=np.float64,
        )

    def rectify(self, left, right):
        return (
            cv2.remap(left, self.left_maps[0], self.left_maps[1], cv2.INTER_LINEAR),
            cv2.remap(right, self.right_maps[0], self.right_maps[1], cv2.INTER_LINEAR),
        )

    def depth(self, disparity):
        """Convert disparity in pixels to z in metres. NaN stays NaN."""
        return self.fx * self.baseline_m / disparity

    def points(self, disparity):
        """Return an H x W x 3 array of x, y, z in metres."""
        z = self.depth(disparity)
        rows, columns = np.indices((self.height, self.width), dtype=np.float32)
        x = (columns - self.cx) * z / self.fx
        y = (rows - self.cy) * z / self.fy
        return np.dstack((x, y, z))

    def expected_error_m(self, distance_m, matching_error_px=0.25):
        """Depth error at a distance, for a given disparity error."""
        return distance_m**2 / (self.fx * self.baseline_m) * matching_error_px


def make_matcher(num_disparities=DEFAULT_NUM_DISPARITIES, block_size=DEFAULT_BLOCK_SIZE):
    """Create a semi-global matcher tuned for 8-bit mono images."""
    return cv2.StereoSGBM_create(
        minDisparity=0,
        numDisparities=num_disparities,
        blockSize=block_size,
        P1=8 * block_size**2,
        P2=32 * block_size**2,
        disp12MaxDiff=1,
        uniquenessRatio=10,
        speckleWindowSize=100,
        speckleRange=2,
        mode=cv2.STEREO_SGBM_MODE_SGBM_3WAY,
    )


def compute_disparity(matcher, left, right):
    """Return disparity in pixels as float32, with NaN where matching failed."""
    disparity = matcher.compute(left, right).astype(np.float32) / DISPARITY_SCALE
    disparity[disparity <= 0] = np.nan
    return disparity


def region_stats(depth, half_size):
    """Summarise depth in a centred square, ignoring failed pixels."""
    height, width = depth.shape
    region = depth[
        height // 2 - half_size : height // 2 + half_size,
        width // 2 - half_size : width // 2 + half_size,
    ]
    valid = region[np.isfinite(region)]
    if valid.size == 0:
        return {"valid_fraction": 0.0, "median_m": None, "p25_m": None, "p75_m": None}
    p25, median, p75 = np.percentile(valid, (25, 50, 75))
    return {
        "valid_fraction": float(valid.size / region.size),
        "median_m": float(median),
        "p25_m": float(p25),
        "p75_m": float(p75),
    }


def region_disparity(disparity, half_size, trim_percent=10):
    """Average disparity in a centred square, finer than SGBM's 1/16 px steps.

    The extreme tails are trimmed first, so stray mismatches don't skew it.
    Returns None when fewer than a quarter of the pixels matched.
    """
    height, width = disparity.shape
    region = disparity[
        height // 2 - half_size : height // 2 + half_size,
        width // 2 - half_size : width // 2 + half_size,
    ]
    valid = region[np.isfinite(region)]
    if valid.size < region.size / 4:
        return None
    low, high = np.percentile(valid, (trim_percent, 100 - trim_percent))
    kept = valid[(valid >= low) & (valid <= high)]
    return float(kept.mean())


def fit_scale_offset(expected_px, measured_px):
    """Fit measured = scale * expected + offset, to tell the two error kinds apart.

    A pure scale error gives offset near 0. A geometry error that shifts every
    point equally gives scale near 1 and a constant offset.
    """
    if len(set(expected_px)) < 2:
        return None
    scale, offset = np.polyfit(np.asarray(expected_px, float), np.asarray(measured_px, float), 1)
    return {"scale": float(scale), "offset_px": float(offset)}


def region_shift(left, right, half_size):
    """Measure the central region's shift between rectified images by correlation.

    This needs no features, so it works on dark or plain targets. Returns
    disparity (positive when the right image is shifted left, as for any point
    in front of the rig), row offset (right minus left, in pixels), and
    OpenCV's response, which falls towards 0 as the match gets unreliable.
    """
    height, width = left.shape
    rows = slice(height // 2 - half_size, height // 2 + half_size)
    columns = slice(width // 2 - half_size, width // 2 + half_size)

    def prepared(image):
        region = image[rows, columns].astype(np.float32)
        return region - cv2.GaussianBlur(region, (0, 0), 3)

    window = cv2.createHanningWindow((2 * half_size, 2 * half_size), cv2.CV_32F)
    (dx, dy), response = cv2.phaseCorrelate(prepared(left), prepared(right), window)
    return {"disparity_px": float(-dx), "row_offset_px": float(dy), "response": float(response)}


def vertical_alignment(left, right):
    """Measure row misalignment between rectified images, from matched features.

    Good rectification puts matching features on the same row. Metric depth
    can't be trusted if they aren't. Returns None with too few matches to judge.
    """
    detector = cv2.ORB_create(nfeatures=2000)
    left_points, left_descriptors = detector.detectAndCompute(left, None)
    right_points, right_descriptors = detector.detectAndCompute(right, None)
    if left_descriptors is None or right_descriptors is None:
        return None

    matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
    offsets = []
    for match in matcher.match(left_descriptors, right_descriptors):
        left_x, left_y = left_points[match.queryIdx].pt
        right_x, right_y = right_points[match.trainIdx].pt
        if match.distance <= MAX_MATCH_DISTANCE and left_x >= right_x:
            offsets.append(abs(left_y - right_y))

    if len(offsets) < MIN_ALIGNMENT_MATCHES:
        return None
    return {
        "matches": len(offsets),
        "median_px": float(np.median(offsets)),
        "p90_px": float(np.percentile(offsets, 90)),
    }


def summarise_measurements(records):
    """Group tape-measure records by true distance into an error table."""
    distances = sorted({record["true_m"] for record in records})
    rows = []
    for distance in distances:
        group = [record for record in records if record["true_m"] == distance]
        medians = [record["median_m"] for record in group if record["median_m"] is not None]
        if not medians:
            rows.append({"true_m": distance, "runs": len(group), "measured_m": None})
            continue
        measured = float(np.median(medians))
        rows.append(
            {
                "true_m": distance,
                "runs": len(group),
                "measured_m": measured,
                "error_m": measured - distance,
                "error_percent": (measured - distance) / distance * 100,
                "frame_noise_m": float(np.median([record["frame_std_m"] for record in group])),
                "valid_fraction": float(np.median([record["valid_fraction"] for record in group])),
            }
        )
    return rows

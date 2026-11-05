"""Decode raw Leap frames.

The Leap labels its stream YUY2, but the bytes are two interleaved 8-bit
images, starting with the left camera. The decode rules match
`getEmbeddedLine` in Python/leapuvc.py and the LeapUVC manual.
"""

from dataclasses import dataclass

import numpy as np

METADATA_BYTES = 12


@dataclass(frozen=True)
class EmbeddedMeta:
    """Metadata from the frame's last 12 bytes.

    `exposure` wraps every 512 and `gain` wraps every 32, so compare them
    with the set value modulo those numbers.
    """

    frame_label: int
    exposure: int
    gain: int


def to_interleaved(raw, width, height, pixel_format="yuvs"):
    """Return an H x 2W uint8 array with the left camera's byte first."""
    data = np.frombuffer(raw, dtype=np.uint8)
    expected = 2 * width * height
    if data.size != expected:
        raise ValueError(f"expected {expected} bytes for {width}x{height}, got {data.size}")
    frame = data.reshape(height, width * 2)
    if pixel_format == "yuvs":
        return frame
    if pixel_format == "2vuy":
        # 2vuy is yuvs with each byte pair swapped.
        swapped = frame.copy()
        swapped[:, ::2] = frame[:, 1::2]
        swapped[:, 1::2] = frame[:, ::2]
        return swapped
    raise ValueError(f"unsupported pixel format {pixel_format!r}")


def deinterleave(interleaved):
    """Split an H x 2W interleaved array into (left, right) H x W images."""
    return interleaved[:, ::2], interleaved[:, 1::2]


def parse_embedded(interleaved):
    """Decode the 12 metadata bytes at the bottom-right of the interleaved frame."""
    tail = [int(value) for value in interleaved[-1, -METADATA_BYTES:]]
    label1 = (tail[6] >> 4) & 0x1
    label2 = ((tail[2] & 0xF) << 4) + (tail[4] & 0xF)
    return EmbeddedMeta(
        frame_label=max(label1, label2) & 0x7F,
        exposure=((tail[6] & 0xF) << 5) + (tail[8] & 0x1F),
        gain=tail[10] & 0x1F,
    )


def best_shift_correlation(left, right, max_shift=64, step=2):
    """Find the horizontal shift where the two images agree best.

    A true stereo pair shows one strong peak. If the second image is really
    chroma, or noise, there is no strong peak.

    Returns (shift_pixels, correlation).
    """
    height, width = left.shape
    band_left = left[height // 4 : 3 * height // 4].astype(np.float32)
    band_right = right[height // 4 : 3 * height // 4].astype(np.float32)

    best_shift, best_score = 0, -1.0
    for shift in range(0, max_shift + 1, step):
        a = band_left[:, shift:]
        b = band_right[:, : width - shift]
        a = a - a.mean()
        b = b - b.mean()
        denominator = np.sqrt(float((a * a).sum()) * float((b * b).sum()))
        if denominator == 0:
            continue
        score = float((a * b).sum()) / denominator
        if score > best_score:
            best_shift, best_score = shift, score
    return best_shift, best_score


def frame_stats(left, right):
    """Summarise a stereo frame for the probe report."""
    shift, correlation = best_shift_correlation(left, right)
    return {
        "left_mean": float(left.mean()),
        "right_mean": float(right.mean()),
        "left_std": float(left.std()),
        "right_std": float(right.std()),
        "min": int(min(left.min(), right.min())),
        "max": int(max(left.max(), right.max())),
        "below_16_fraction": float((left < 16).mean()),
        "above_235_fraction": float((left > 235).mean()),
        "mean_abs_left_right": float(
            np.abs(left.astype(np.int16) - right.astype(np.int16)).mean()
        ),
        "best_shift": shift,
        "best_correlation": correlation,
    }

import numpy as np
import pytest

from leapmac import frames

WIDTH, HEIGHT = 8, 4


def stereo_pair(seed=1):
    random = np.random.default_rng(seed)
    left = random.integers(0, 256, (HEIGHT, WIDTH), dtype=np.uint8)
    right = random.integers(0, 256, (HEIGHT, WIDTH), dtype=np.uint8)
    return left, right


def interleave(left, right):
    frame = np.empty((HEIGHT, WIDTH * 2), dtype=np.uint8)
    frame[:, ::2] = left
    frame[:, 1::2] = right
    return frame


def test_yuvs_round_trip():
    left, right = stereo_pair()
    interleaved = frames.to_interleaved(interleave(left, right).tobytes(), WIDTH, HEIGHT, "yuvs")
    decoded_left, decoded_right = frames.deinterleave(interleaved)
    assert np.array_equal(decoded_left, left)
    assert np.array_equal(decoded_right, right)


def test_2vuy_swaps_each_byte_pair():
    left, right = stereo_pair()
    swapped = interleave(right, left).tobytes()
    interleaved = frames.to_interleaved(swapped, WIDTH, HEIGHT, "2vuy")
    decoded_left, decoded_right = frames.deinterleave(interleaved)
    assert np.array_equal(decoded_left, left)
    assert np.array_equal(decoded_right, right)


def test_wrong_size_is_rejected():
    with pytest.raises(ValueError):
        frames.to_interleaved(bytes(10), WIDTH, HEIGHT)


def test_unknown_pixel_format_is_rejected():
    with pytest.raises(ValueError):
        frames.to_interleaved(bytes(2 * WIDTH * HEIGHT), WIDTH, HEIGHT, "BGRA")


def test_parse_embedded_uses_the_bottom_right_corner():
    frame = np.zeros((HEIGHT, WIDTH * 2), dtype=np.uint8)
    # Exposure 464 is (14 << 5) + 16. Bit 4 of byte 6 sets label1.
    tail = [0, 0, 0x0A, 0, 0x03, 0, 0x1E, 0, 16, 0, 20, 0]
    frame[-1, -12:] = tail

    meta = frames.parse_embedded(frame)

    assert meta.exposure == 464
    assert meta.gain == 20
    # label2 is (0xA << 4) + 3 = 163, and 163 & 0x7F = 35.
    assert meta.frame_label == 35


def test_parse_embedded_masks_high_bits():
    frame = np.zeros((HEIGHT, WIDTH * 2), dtype=np.uint8)
    frame[-1, -12:] = [0xFF] * 12
    meta = frames.parse_embedded(frame)
    assert meta.exposure == 511
    assert meta.gain == 31
    assert meta.frame_label == 0x7F


def test_best_shift_finds_a_known_disparity():
    random = np.random.default_rng(2)
    height, width, disparity = 120, 200, 10
    left = random.integers(0, 256, (height, width), dtype=np.uint8)
    right = np.zeros_like(left)
    right[:, : width - disparity] = left[:, disparity:]

    shift, correlation = frames.best_shift_correlation(left, right, max_shift=32, step=2)

    assert shift == disparity
    assert correlation > 0.95


def test_best_shift_scores_unrelated_images_low():
    random = np.random.default_rng(3)
    left = random.integers(0, 256, (120, 200), dtype=np.uint8)
    right = random.integers(0, 256, (120, 200), dtype=np.uint8)
    _, correlation = frames.best_shift_correlation(left, right, max_shift=32)
    assert correlation < 0.1


def test_frame_stats_handles_flat_images():
    flat = np.full((40, 80), 128, dtype=np.uint8)
    stats = frames.frame_stats(flat, flat)
    assert stats["left_std"] == 0
    assert stats["mean_abs_left_right"] == 0
    assert stats["min"] == stats["max"] == 128

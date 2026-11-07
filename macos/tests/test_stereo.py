import cv2
import numpy as np
import pytest

from leapmac import stereo

WIDTH, HEIGHT = 640, 480


def camera(fx, cx, cy):
    return {
        "extrinsics": {
            "cameraMatrix": np.array([[fx, 0, cx], [0, fx, cy], [0, 0, 1]], dtype=np.float32),
            "r": np.eye(3, dtype=np.float32),
        },
        "intrinsics": {"distCoeffs": np.zeros(8, dtype=np.float32)},
    }


def calibration(baseline=40.0):
    return {
        "baseline": baseline,
        "left": camera(130.0, 325.0, 235.0),
        "right": camera(134.0, 315.0, 245.0),
    }


def rig():
    return stereo.StereoRig.from_calibration(calibration(), (WIDTH, HEIGHT))


def textured(height=HEIGHT, width=WIDTH):
    random = np.random.default_rng(1)
    coarse = random.integers(0, 256, (height // 4 + 1, width // 4 + 1), dtype=np.uint8)
    return cv2.resize(coarse, (width, height), interpolation=cv2.INTER_LINEAR)


def test_baseline_is_converted_to_metres():
    assert rig().baseline_m == pytest.approx(0.04)


def test_cameras_share_one_matrix():
    built = rig()
    assert built.fx == pytest.approx(132.0)
    assert built.fy == pytest.approx(132.0)
    assert (built.cx, built.cy) == (320.0, 240.0)


def test_vertical_scale_follows_binned_modes():
    built = stereo.StereoRig.from_calibration(calibration(), (640, 240))
    assert built.fy == pytest.approx(66.0)
    assert built.cy == 120.0


@pytest.mark.parametrize("baseline", [4.0, 0.04, 400.0])
def test_a_wrong_baseline_unit_is_rejected(baseline):
    with pytest.raises(ValueError):
        stereo.StereoRig.from_calibration(calibration(baseline), (WIDTH, HEIGHT))


def test_rectification_moves_each_principal_point_to_the_shared_centre():
    built = rig()
    image = np.zeros((HEIGHT, WIDTH), dtype=np.uint8)
    cv2.circle(image, (325, 235), 3, 255, -1)
    other = np.zeros((HEIGHT, WIDTH), dtype=np.uint8)
    cv2.circle(other, (315, 245), 3, 255, -1)

    left, right = built.rectify(image, other)

    assert np.argwhere(left == left.max()).mean(axis=0) == pytest.approx([240, 320], abs=1.5)
    assert np.argwhere(right == right.max()).mean(axis=0) == pytest.approx([240, 320], abs=1.5)


def test_depth_follows_focal_length_times_baseline():
    built = rig()
    assert built.depth(np.float32(5.28)) == pytest.approx(1.0, rel=1e-3)


def test_depth_keeps_failed_pixels_as_nan():
    assert np.isnan(rig().depth(np.array([np.nan], dtype=np.float32))[0])


def test_points_place_the_centre_on_the_axis():
    built = rig()
    disparity = np.full((HEIGHT, WIDTH), 5.28, dtype=np.float32)
    points = built.points(disparity)

    assert points.shape == (HEIGHT, WIDTH, 3)
    assert points[240, 320] == pytest.approx([0, 0, 1], abs=1e-3)
    assert points[240, 452, 0] == pytest.approx(1.0, rel=1e-3)
    assert points[372, 320, 1] == pytest.approx(1.0, rel=1e-3)


def test_points_match_opencv_reprojection():
    built = rig()
    disparity = np.full((HEIGHT, WIDTH), 10.0, dtype=np.float32)
    ours = built.points(disparity)
    theirs = cv2.reprojectImageTo3D(disparity, built.Q)
    assert ours[100, 200] == pytest.approx(theirs[100, 200], abs=1e-3)


def test_expected_error_grows_with_the_square_of_distance():
    built = rig()
    assert built.expected_error_m(2.0) == pytest.approx(built.expected_error_m(1.0) * 4)


def test_matcher_recovers_a_known_shift():
    left = textured()
    right = np.roll(left, -20, axis=1)

    disparity = stereo.compute_disparity(stereo.make_matcher(), left, right)

    assert np.nanmedian(disparity[120:360, 200:500]) == pytest.approx(20, abs=0.5)


def test_flat_images_report_no_valid_disparity_as_nan():
    flat = np.full((HEIGHT, WIDTH), 128, dtype=np.uint8)
    disparity = stereo.compute_disparity(stereo.make_matcher(), flat, flat)
    assert disparity.dtype == np.float32
    assert not np.any(disparity <= 0)


def test_region_stats_ignore_failed_pixels():
    depth = np.full((100, 100), np.nan, dtype=np.float32)
    depth[40:50, 40:60] = 2.0

    stats = stereo.region_stats(depth, half_size=10)

    assert stats["median_m"] == pytest.approx(2.0)
    assert stats["valid_fraction"] == pytest.approx(0.5)


def test_region_stats_handle_an_empty_region():
    depth = np.full((100, 100), np.nan, dtype=np.float32)
    assert stereo.region_stats(depth, half_size=10)["median_m"] is None


def test_region_shift_reports_disparity_and_row_offset():
    left = textured()
    right = np.roll(np.roll(left, -12, axis=1), 3, axis=0)

    shift = stereo.region_shift(left, right, half_size=80)

    assert shift["disparity_px"] == pytest.approx(12, abs=0.3)
    assert shift["row_offset_px"] == pytest.approx(3, abs=0.3)
    assert shift["response"] > 0.3


def test_rotation_modes_differ_only_when_rotation_is_present():
    tilted = calibration()
    angle = 0.02
    tilted["left"]["extrinsics"]["r"] = np.array(
        [[np.cos(angle), 0, np.sin(angle)], [0, 1, 0], [-np.sin(angle), 0, np.cos(angle)]],
        dtype=np.float32,
    )
    factory = stereo.StereoRig.from_calibration(tilted, (WIDTH, HEIGHT))
    transposed = stereo.StereoRig.from_calibration(tilted, (WIDTH, HEIGHT), rotation="transposed")
    identity = stereo.StereoRig.from_calibration(calibration(), (WIDTH, HEIGHT), rotation="identity")
    plain = stereo.StereoRig.from_calibration(calibration(), (WIDTH, HEIGHT))

    assert not np.array_equal(factory.left_maps[0], transposed.left_maps[0])
    assert np.array_equal(identity.left_maps[0], plain.left_maps[0])


def test_unknown_rotation_mode_is_rejected():
    with pytest.raises(ValueError):
        stereo.StereoRig.from_calibration(calibration(), (WIDTH, HEIGHT), rotation="sideways")


def test_offsets_shift_one_cameras_rectified_image():
    built = stereo.StereoRig.from_calibration(calibration(), (WIDTH, HEIGHT), offsets={"right": (5.0, 0.0)})
    image = np.zeros((HEIGHT, WIDTH), dtype=np.uint8)
    cv2.circle(image, (315, 245), 3, 255, -1)

    _, right = built.rectify(image, image)

    assert np.argwhere(right == right.max()).mean(axis=0) == pytest.approx([240, 325], abs=1.5)


def test_alignment_is_near_zero_for_a_pure_horizontal_shift():
    left = textured()
    right = np.roll(left, -20, axis=1)

    alignment = stereo.vertical_alignment(left, right)

    assert alignment["matches"] >= 10
    assert alignment["median_px"] < 1.0


def test_alignment_detects_a_vertical_offset():
    left = textured()
    right = np.roll(np.roll(left, -20, axis=1), 6, axis=0)

    assert stereo.vertical_alignment(left, right)["median_px"] > 4.0


def test_alignment_gives_up_on_a_featureless_image():
    flat = np.full((HEIGHT, WIDTH), 128, dtype=np.uint8)
    assert stereo.vertical_alignment(flat, flat) is None


def record(true_m, median_m, std=0.01, valid=0.9):
    return {"true_m": true_m, "median_m": median_m, "frame_std_m": std, "valid_fraction": valid}


def test_summary_reports_error_per_distance():
    rows = stereo.summarise_measurements([record(1.0, 1.05), record(1.0, 1.03), record(2.0, 1.8)])

    assert [row["true_m"] for row in rows] == [1.0, 2.0]
    assert rows[0]["runs"] == 2
    assert rows[0]["measured_m"] == pytest.approx(1.04)
    assert rows[1]["error_m"] == pytest.approx(-0.2)
    assert rows[1]["error_percent"] == pytest.approx(-10)


def test_summary_marks_a_distance_with_no_depth():
    rows = stereo.summarise_measurements([record(3.0, None, std=None)])
    assert rows[0]["measured_m"] is None

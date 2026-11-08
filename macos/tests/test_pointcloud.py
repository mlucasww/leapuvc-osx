import numpy as np
import pytest

from leapmac import pointcloud, stereo
from test_stereo import HEIGHT, WIDTH, camera, textured


def aligned_calibration():
    """Two identical cameras, so rectification leaves a pure horizontal shift."""
    return {"baseline": 40.0, "left": camera(132.0, 320.0, 240.0), "right": camera(132.0, 320.0, 240.0)}


def rig(disparity_offset_px=0.0):
    return stereo.StereoRig.from_calibration(
        aligned_calibration(), (WIDTH, HEIGHT), disparity_offset_px=disparity_offset_px
    )


def shifted_pair(disparity_px):
    """A left/right frame where every point sits at the same disparity."""
    left = textured()
    right = np.roll(left, -disparity_px, axis=1)
    return np.stack((left, right))


def builder(**settings):
    return pointcloud.CloudBuilder(rig(), pointcloud.CloudSettings(**settings))


def test_a_flat_scene_gives_points_at_its_depth():
    built = builder()
    cloud = built.process(shifted_pair(16))

    expected = built.rig.fx * built.rig.baseline_m / 16
    assert cloud.size > 10_000
    assert np.median(cloud.points[:, 2]) == pytest.approx(expected, rel=0.02)


def test_points_lie_along_each_pixels_ray():
    built = builder(stride=1)
    cloud = built.process(shifted_pair(16))

    # Reproject each point into the image: it must land on a valid pixel.
    columns = cloud.points[:, 0] / cloud.points[:, 2] * built.rig.fx + built.rig.cx
    rows = cloud.points[:, 1] / cloud.points[:, 2] * built.rig.fy + built.rig.cy
    assert np.allclose(columns, np.round(columns), atol=1e-3)
    assert np.allclose(rows, np.round(rows), atol=1e-3)
    assert np.isfinite(cloud.depth[rows.round().astype(int), columns.round().astype(int)]).all()


def test_points_outside_the_depth_range_are_dropped():
    pair = shifted_pair(16)
    depth = rig().fx * rig().baseline_m / 16

    assert builder(max_depth_m=depth * 0.9).process(pair).size == 0
    assert builder(min_depth_m=depth * 1.1, max_depth_m=depth * 2).process(pair).size == 0
    assert np.isnan(builder(max_depth_m=depth * 0.9).process(pair).depth).all()


def test_stride_subsamples_the_cloud():
    pair = shifted_pair(16)
    full = builder(stride=1).process(pair).size
    quarter = builder(stride=2).process(pair).size
    assert quarter == pytest.approx(full / 4, rel=0.05)


def test_brightness_comes_from_the_left_image():
    cloud = builder(stride=1).process(shifted_pair(16))
    built = builder(stride=1)
    columns = (cloud.points[:, 0] / cloud.points[:, 2] * built.rig.fx + built.rig.cx).round().astype(int)
    rows = (cloud.points[:, 1] / cloud.points[:, 2] * built.rig.fy + built.rig.cy).round().astype(int)
    assert np.array_equal(cloud.brightness, cloud.left[rows, columns])


def test_disparity_offset_moves_the_cloud():
    pair = shifted_pair(16)
    plain = pointcloud.CloudBuilder(rig()).process(pair)
    corrected = pointcloud.CloudBuilder(rig(disparity_offset_px=1.0)).process(pair)
    assert np.median(corrected.points[:, 2]) / np.median(plain.points[:, 2]) == pytest.approx(16 / 15, rel=0.01)


def test_a_featureless_frame_gives_an_empty_cloud():
    flat = np.full((2, HEIGHT, WIDTH), 128, dtype=np.uint8)
    cloud = builder().process(flat)
    assert cloud.size == 0
    assert cloud.points.shape == (0, 3)


def test_timings_are_reported():
    cloud = builder().process(shifted_pair(16))
    assert set(cloud.timings_ms) == {"rectify", "match", "points", "total"}
    assert cloud.timings_ms["total"] >= cloud.timings_ms["match"]


@pytest.mark.parametrize("settings", [{"stride": 0}, {"min_depth_m": 0}, {"min_depth_m": 2, "max_depth_m": 1}])
def test_bad_settings_are_rejected(settings):
    with pytest.raises(ValueError):
        pointcloud.CloudSettings(**settings)


def test_rate_meter_counts_over_its_window():
    now = [0.0]
    meter = pointcloud.RateMeter(window_s=1.0, clock=lambda: now[0])
    for _ in range(21):
        meter.tick()
        now[0] += 0.05
    assert meter.rate == pytest.approx(20.0)


def test_rate_meter_reads_zero_until_two_ticks():
    meter = pointcloud.RateMeter()
    assert meter.rate == 0.0
    meter.tick()
    assert meter.rate == 0.0

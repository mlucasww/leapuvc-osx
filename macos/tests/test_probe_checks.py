import probe_leap
from leapmac.probe_decision import INCONCLUSIVE, PASS


def stereo_step():
    return {
        "width": 640,
        "height": 480,
        "pixel_format": "yuvs",
        "bytes_per_row": 1280,
        "stats": {
            "left_std": 42.9,
            "right_std": 41.0,
            "mean_abs_left_right": 21.4,
            "best_correlation": 0.84,
            "below_16_fraction": 0.22,
            "above_235_fraction": 0.0,
        },
    }


def findings(**overrides):
    values = {
        "gain_plain_form_works": False,
        "gain_selector_form_works": False,
        "exposure_metadata_matches_microseconds": False,
    }
    values.update(overrides)
    return values


def test_exact_exposure_metadata_proves_the_bytes_without_gain():
    outcome = probe_leap.check_bytes(
        {"exposure_8000": stereo_step()},
        (640, 480),
        findings(exposure_metadata_matches_microseconds=True),
    )
    assert outcome["status"] == PASS


def test_stereo_match_alone_stays_inconclusive():
    outcome = probe_leap.check_bytes({"exposure_8000": stereo_step()}, (640, 480), findings())
    assert outcome["status"] == INCONCLUSIVE


def test_evaluate_controls_reads_wrapped_exposure_metadata():
    steps = {
        "exposure_2000": {"meta": {"exposure": 464, "gain": 16}, "stats": {"left_mean": 42.0}},
        "exposure_8000": {"meta": {"exposure": 320, "gain": 16}, "stats": {"left_mean": 55.0}},
    }
    outcome = probe_leap.evaluate_controls(steps, {})
    assert outcome["exposure_metadata_matches_microseconds"] is True
    assert outcome["exposure_metadata_changed"] is True
    assert outcome["gain_plain_form_works"] is False

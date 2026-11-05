from leapmac.probe_decision import BLOCKED, FAIL, INCONCLUSIVE, PASS, decide


def test_all_passing_gives_path_a():
    decision = decide(PASS, PASS, PASS, PASS)
    assert decision["path"] == "A"
    assert decision["calibration"] == "device blob"
    assert decision["open_checks"] == []


def test_control_failure_gives_path_b():
    assert decide(PASS, PASS, FAIL, PASS)["path"] == "B"


def test_frame_failures_give_path_c():
    assert decide(FAIL, "skipped", "skipped", PASS)["path"] == "C"
    assert decide(PASS, FAIL, PASS, PASS)["path"] == "C"


def test_path_c_takes_priority_over_path_b():
    assert decide(PASS, FAIL, FAIL, PASS)["path"] == "C"


def test_calibration_failure_only_changes_calibration():
    decision = decide(PASS, PASS, PASS, FAIL)
    assert decision["path"] == "A"
    assert decision["calibration"] == "checkerboard"


def test_blocked_never_selects_a_fallback():
    decision = decide(BLOCKED, "skipped", "skipped", "skipped")
    assert decision["path"] is None
    assert decision["open_checks"] == ["1. device visible"]


def test_inconclusive_results_are_flagged_as_provisional():
    decision = decide(PASS, INCONCLUSIVE, PASS, PASS)
    assert decision["path"] == "A"
    assert decision["open_checks"] == ["2. bytes intact"]
    assert "Provisional" in decision["summary"]

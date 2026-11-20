"""Tests for multi-period loop detection in agent_loop._detect_loop_period()."""
from agent_loop import _detect_loop_period


class TestDetectLoopPeriod:
    def test_period_1_detected(self):

        assert _detect_loop_period(["a", "a"]) == 1

    def test_period_1_with_history(self):
        assert _detect_loop_period(["x", "y", "a", "a"]) == 1

    def test_period_2_detected(self):

        assert _detect_loop_period(["a", "b", "a", "b"]) == 2

    def test_period_3_detected(self):

        assert _detect_loop_period(["a", "b", "c", "a", "b", "c"]) == 3

    def test_no_repeat(self):
        assert _detect_loop_period(["a", "b", "c", "d"]) is None

    def test_too_short_for_period_2(self):

        assert _detect_loop_period(["a", "b", "a"]) is None

    def test_too_short_for_any(self):
        assert _detect_loop_period(["a"]) is None
        assert _detect_loop_period([]) is None

    def test_period_1_preferred_over_period_2(self):

        assert _detect_loop_period(["a", "a", "a", "a"]) == 1

    def test_period_2_not_confused_with_period_1(self):

        sigs = ["a", "b", "a", "b"]
        period = _detect_loop_period(sigs)
        assert period == 2

        assert sigs[-1] != sigs[-2]

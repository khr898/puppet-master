from mathutil import clamp, double


def test_double():
    assert double(4) == 8


def test_clamp_limits_both_ends():
    assert clamp(15, 0, 10) == 10
    assert clamp(-5, 0, 10) == 0
    assert clamp(7, 0, 10) == 7

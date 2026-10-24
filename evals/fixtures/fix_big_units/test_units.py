import pytest
from units import celsius_to_fahrenheit, fahrenheit_to_celsius, km_to_miles


def test_celsius_to_fahrenheit():
    assert celsius_to_fahrenheit(100) == 212
    assert celsius_to_fahrenheit(0) == 32


def test_round_trip():
    assert fahrenheit_to_celsius(celsius_to_fahrenheit(37)) == pytest.approx(37)


def test_other_conversions_unchanged():
    assert km_to_miles(10) == pytest.approx(6.21371)

import pytest
from units import celsius_to_kelvin, km_to_miles


def test_celsius_to_kelvin():
    assert celsius_to_kelvin(0) == pytest.approx(273.15)
    assert celsius_to_kelvin(-273.15) == pytest.approx(0)


def test_rejects_non_numbers():
    with pytest.raises(TypeError):
        celsius_to_kelvin("hot")


def test_other_conversions_unchanged():
    assert km_to_miles(10) == pytest.approx(6.21371)

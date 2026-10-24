from roman import to_roman


def test_simple_numerals():
    assert to_roman(3) == "III"
    assert to_roman(4) == "IV"
    assert to_roman(9) == "IX"


def test_large_numeral():
    assert to_roman(1994) == "MCMXCIV"

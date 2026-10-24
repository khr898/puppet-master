from lists import dedupe


def test_dedupe_keeps_first_occurrences_in_order():
    assert dedupe([3, 1, 3, 2, 1]) == [3, 1, 2]


def test_dedupe_of_nothing():
    assert dedupe([]) == []

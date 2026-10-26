import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
import hallucinations as hx  # noqa: E402

from puppet_master.read import (  # noqa: E402
    Claim,
    _norm,
    filter_claims,
    number_lines,
    parse_claims,
    verify_claim,
)


def test_number_lines_right_aligns_and_honours_start():
    assert number_lines(["a", "b"]) == "    1| a\n    2| b"
    assert number_lines(["x"], start=120) == "  120| x"


def test_norm_collapses_all_whitespace():
    assert _norm("  a \t b\n\nc ") == "a b c"


def test_verify_claim_exact_match():
    c = Claim("t", "f", 2, "beta")
    assert verify_claim(c, {"f": (1, ["alpha", "beta", "gamma"])})


def test_verify_claim_unknown_file_is_false():
    assert not verify_claim(Claim("t", "nope", 1, "a"), {"f": (1, ["a"])})


@pytest.mark.parametrize("line", [0, -3, 4])
def test_verify_claim_line_out_of_range_is_false(line):
    assert not verify_claim(Claim("t", "f", line, "a"), {"f": (1, ["a", "b", "c"])})


def test_verify_claim_empty_quote_is_false():
    assert not verify_claim(Claim("t", "f", 1, "  "), {"f": (1, ["a"])})


def test_verify_claim_window_is_two_lines_each_way():
    lines = ["x"] * 4 + ["needle"] + ["x"] * 4  # needle on line 5
    assert verify_claim(Claim("t", "f", 7, "needle"), {"f": (1, lines)})
    assert not verify_claim(Claim("t", "f", 8, "needle"), {"f": (1, lines)})


def test_verify_claim_uses_original_line_numbers_of_a_slice():
    files = {"f": (10, ["ten", "eleven", "twelve"])}
    assert verify_claim(Claim("t", "f", 11, "eleven"), files)
    assert not verify_claim(Claim("t", "f", 2, "eleven"), files)  # slice starts at line 10
    assert not verify_claim(Claim("t", "f", 13, "ten"), files)  # past the end of the slice


def test_parse_claims_keeps_valid_and_counts_malformed():
    raw = [
        {"text": "a", "file": "f", "line": 3, "quote": "q"},
        {"text": "b", "file": "f", "line": "4", "quote": "q"},  # numeric string is coerced
        {"text": "c", "file": "f", "line": "four", "quote": "q"},
        {"text": "d", "file": "f", "line": True, "quote": "q"},  # bool is not an int
        {"text": "e", "file": "f", "line": 1},
        {"text": 5, "file": "f", "line": 1, "quote": "q"},
        {"text": "f", "file": "f", "line": 1.5, "quote": "q"},
        {"text": "g", "file": "f", "quote": "q"},  # no line
        "not a dict",
    ]
    claims, malformed = parse_claims(raw)
    assert [c.line for c in claims] == [3, 4]
    assert malformed == 7


def test_filter_claims_counts_unverified_as_stripped():
    files = {"f": (1, ["a", "b"])}
    claims = [Claim("ok", "f", 1, "a"), Claim("bad", "f", 1, "zzz")]
    kept, stripped = filter_claims(claims, files)
    assert kept == [claims[0]]
    assert stripped == 1


def test_fixture_has_enough_adversarial_cases():
    assert len(hx.BAD) >= 10
    assert len(hx.GOOD) >= 3


def test_every_bad_claim_is_stripped_and_every_good_one_kept():
    raw = [c for _, c in hx.BAD + hx.GOOD]
    parsed, malformed = parse_claims(raw)
    kept, unverified = filter_claims(parsed, hx.FILES)
    assert len(kept) == len(hx.GOOD)
    assert malformed + unverified == len(hx.BAD)


@pytest.mark.parametrize("name,raw", hx.BAD, ids=[n for n, _ in hx.BAD])
def test_each_bad_claim_is_removed(name, raw):
    parsed, malformed = parse_claims([raw])
    kept, unverified = filter_claims(parsed, hx.FILES)
    assert kept == []
    assert malformed + unverified == 1


@pytest.mark.parametrize("name,raw", hx.GOOD, ids=[n for n, _ in hx.GOOD])
def test_each_good_claim_is_kept(name, raw):
    parsed, malformed = parse_claims([raw])
    kept, unverified = filter_claims(parsed, hx.FILES)
    assert len(kept) == 1
    assert malformed == unverified == 0

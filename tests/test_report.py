import json

from puppet_master.report import cap, est_tokens

LIMIT_CHARS = 500 * 4


def test_est_tokens_is_ceil_of_quarter_length():
    assert est_tokens("") == 0
    assert est_tokens("abcd") == 1
    assert est_tokens("abcde") == 2


def test_small_dict_is_unchanged():
    d = {"answer": "ok", "claims": [{"text": "a"}], "stripped": 0}
    assert cap(d) == d
    assert "truncated" not in cap(d)


def test_many_claims_are_trimmed_to_fit():
    d = {"answer": "x", "claims": [{"text": "claim", "line": i} for i in range(100)]}
    out = cap(d)
    assert len(json.dumps(out)) <= LIMIT_CHARS
    assert out["truncated"] is True
    assert 0 < len(out["claims"]) < 100
    assert out["claims"] == d["claims"][: len(out["claims"])]  # trims from the end


def test_huge_string_is_trimmed():
    out = cap({"answer": "a" * 10_000})
    assert len(json.dumps(out)) <= LIMIT_CHARS
    assert out["truncated"] is True
    assert out["answer"].endswith("…")


def test_longest_list_is_trimmed_first():
    d = {"claims": [1] * 5, "notes": ["n" * 50] * 60}
    out = cap(d)
    assert len(out["claims"]) == 5
    assert len(out["notes"]) < 60


def test_protected_keys_are_never_removed_or_altered():
    d = {
        "error": "e",
        "patch_id": "abc123",
        "patch_path": "/tmp/x.patch",
        "passed": False,
        "applied": False,
        "answer": "a" * 10_000,
        "failures": ["f" * 100] * 100,
    }
    out = cap(d)
    for key in ("error", "patch_id", "patch_path", "passed", "applied"):
        assert out[key] == d[key]
    assert len(json.dumps(out)) <= LIMIT_CHARS


def test_custom_max_tokens():
    out = cap({"answer": "a" * 1000}, max_tokens=50)
    assert len(json.dumps(out)) <= 200


def test_input_dict_is_not_mutated():
    d = {"claims": [{"text": "c"}] * 200}
    cap(d)
    assert len(d["claims"]) == 200

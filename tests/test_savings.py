import json
from pathlib import Path

import httpx
import respx
from test_eval import EVALS, URL, flint_up, model  # noqa: F401  (flint_up is an autouse fixture)

from flint_bridge import eval as flint_eval
from flint_bridge import savings, server
from flint_bridge.config import Config
from flint_bridge.report import compact, est_tokens, factor_base

BIG_FILE = "x = 1\n" * 2000  # ~3000 tokens


def read_result(file: str, line: int = 1, claims: int = 1) -> dict:
    return {
        "answer": "short answer",
        "claims": [{"text": "t", "file": file, "line": line} for _ in range(claims)],
    }


# --- reads ---------------------------------------------------------------------------------


def test_a_read_saves_the_file_tokens_minus_the_report_and_the_cited_lines(tmp_path):
    f = tmp_path / "big.py"
    f.write_text(BIG_FILE)
    result = read_result(str(f))
    s = savings.read_savings([f], result, ok=True)
    assert s["baseline"] == est_tokens(BIG_FILE)
    assert s["with_flint"] == est_tokens(compact(factor_base(result))) + s["verify"]
    assert s["verify"] > 0
    assert s["saved"] == s["baseline"] - s["with_flint"] > 0


def test_a_small_file_costs_more_than_it_saves(tmp_path):
    f = tmp_path / "tiny.py"
    f.write_text("x = 1\n")
    assert savings.read_savings([f], read_result(str(f)), ok=True)["saved"] < 0


def test_a_wrong_read_falls_back_to_claude_reading_the_file(tmp_path):
    f = tmp_path / "big.py"
    f.write_text(BIG_FILE)
    good = savings.read_savings([f], read_result(str(f)), ok=True)
    bad = savings.read_savings([f], read_result(str(f)), ok=False)
    assert bad["with_flint"] == good["with_flint"] + good["baseline"]
    assert bad["saved"] < 0


def test_an_errored_read_still_costs_the_fallback(tmp_path):
    f = tmp_path / "big.py"
    f.write_text(BIG_FILE)
    s = savings.read_savings([f], {"error": "flint_offline"}, ok=False)
    assert s["verify"] == 0
    assert s["saved"] < 0


# --- writes --------------------------------------------------------------------------------

PATCH = "--- a/m.py\n+++ b/m.py\n@@ -1 +1,2 @@\n x\n+" + "y" * 400 + "\n"


def test_a_write_saves_output_tokens_which_cost_more_than_input():
    s = savings.write_savings(["x = 1\n" * 100], PATCH, {"diffstat": "1 file"}, passed=True)
    assert s["baseline_out"] == est_tokens("y" * 400)
    assert s["baseline"] == est_tokens("x = 1\n" * 100) + savings.OUT_WEIGHT * s["baseline_out"]
    assert s["with_flint"] == est_tokens(PATCH) + est_tokens(json.dumps({"diffstat": "1 file"}))
    assert s["saved"] == s["baseline"] - s["with_flint"]


def test_a_failed_write_check_wastes_the_attempt():
    ok = savings.write_savings(["a\n"], PATCH, {}, passed=True)
    failed = savings.write_savings(["a\n"], PATCH, {}, passed=False)
    assert failed["with_flint"] == ok["with_flint"] + ok["baseline"]
    assert failed["saved"] < 0


def test_a_write_with_no_patch_costs_the_fallback():
    s = savings.write_savings(["a\n"], None, {"error": "unverifiable_output"}, passed=False)
    assert s["saved"] < 0


# --- totals --------------------------------------------------------------------------------


def test_totals_add_up_and_give_a_percentage():
    rows = [{"baseline": 1000, "with_flint": 200}, {"baseline": 500, "with_flint": 600}]
    t = savings.totals(rows)
    assert t == {"baseline": 1500, "with_flint": 800, "saved": 700, "saved_pct": 700 / 1500 * 100}


def test_totals_of_nothing_is_none():
    assert savings.totals([])["saved_pct"] is None


# --- wired into the eval -------------------------------------------------------------------


async def test_every_eval_task_carries_a_savings_row():
    respx.post(URL).mock(side_effect=model())
    res = await flint_eval.run_eval(Config(), EVALS, pid_fn=lambda: None)
    for row in res["reads"] + res["writes"]:
        assert {"baseline", "with_flint", "saved"} <= row["savings"].keys()
    assert {"read", "write", "all"} <= res["summary"]["savings"].keys()
    assert res["summary"]["savings"]["all"]["baseline"] > 0


async def test_a_dead_server_shows_negative_savings_not_a_crash():
    respx.post(URL).mock(side_effect=httpx.ConnectError("refused"))
    res = await flint_eval.run_eval(Config(), EVALS, pid_fn=lambda: None)
    assert res["summary"]["savings"]["all"]["saved"] <= 0


async def test_flint_savings_prints_each_task_and_the_total(tmp_path, capsys):
    respx.post(URL).mock(side_effect=model())
    argv = ["--evals-dir", str(EVALS), "--results-dir", str(tmp_path), "--label", "t"]
    assert await savings.amain(argv) == 0
    out = capsys.readouterr().out
    assert "TOTAL" in out and "cap-protected-keys" in out and "roman-numerals" in out
    assert (Path(tmp_path) / "t.json").exists()


def test_old_results_without_savings_still_compare(tmp_path, capsys):
    old = {"summary": {"read": {}, "write": {}, "speed": {}, "memory": {}, "run": {}}}
    (tmp_path / "a.json").write_text(json.dumps(old))
    assert savings.render_saved(old) == "no savings data in this run; re-run with flint-savings"
    assert server  # imported for the autouse fixture's monkeypatch target

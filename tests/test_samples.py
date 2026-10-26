import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
import respx

from puppet_master import samples, server, stats
from puppet_master.config import Config
from puppet_master.read import READ_MAX_TOKENS, puppet_read_impl

BASE = Config().base_url
URL = f"{BASE}/v1/chat/completions"
GOOD = "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"
BAD = "def add(a, b):\n    return a - b\n"


@pytest.fixture(autouse=True)
def flint_up(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "mem_pressure", lambda: 10)
    with respx.mock:
        respx.get(f"{BASE}/health").mock(return_value=httpx.Response(200, json={}))
        respx.get(f"{BASE}/props").mock(
            return_value=httpx.Response(200, json={"default_generation_settings": {"n_ctx": 16384}})
        )
        yield


def reply(content: str | dict, **usage) -> httpx.Response:
    text = content if isinstance(content, str) else json.dumps(content)
    body = {"choices": [{"message": {"content": text}}], "usage": usage}
    return httpx.Response(200, json=body)


def edit(path: Path, content: str) -> dict:
    return {"files": [{"path": str(path), "content": content}], "notes": "n"}


def sample_rows(root: Path) -> list[dict]:
    rows = []
    for f in sorted((root / ".puppet-master" / "samples").glob("*.jsonl")):
        rows += [json.loads(x) for x in f.read_text().splitlines()]
    return rows


@pytest.fixture
def source(git_repo) -> Path:
    return git_repo / "app.py"


# --- capture -------------------------------------------------------------------------------


async def test_read_writes_one_sample_with_prompt_and_raw_output(git_repo, source):
    raw = {"answer": "adds", "claims": []}
    respx.post(URL).mock(return_value=reply(raw))
    await server.puppet_read([str(source)], "what does add do?")
    [row] = sample_rows(git_repo)
    assert row["tool"] == "puppet_read"
    assert "JSON only" in row["system"]
    assert "what does add do?" in row["user"]
    assert json.loads(row["raw_output"]) == raw
    assert row["verdict"] == "verified"


async def test_stripped_claims_show_in_the_verdict(git_repo, source):
    bad = {"text": "t", "file": str(source), "line": 1, "quote": "not in the file"}
    respx.post(URL).mock(return_value=reply({"answer": "a", "claims": [bad]}))
    await server.puppet_read([str(source)], "q")
    assert sample_rows(git_repo)[0]["verdict"] == "stripped:1"


async def test_unverifiable_output_records_both_attempts(git_repo, source):
    respx.post(URL).mock(return_value=reply("not json"))
    await server.puppet_read([str(source)], "q")
    rows = sample_rows(git_repo)
    assert [r["verdict"] for r in rows] == ["unverifiable", "unverifiable"]
    assert rows[0]["raw_output"] == "not json"


async def test_write_apply_and_reject_record_their_verdicts(git_repo, source):
    respx.post(URL).mock(return_value=reply(edit(source, BAD)))
    failed = await server.puppet_write("break it", [str(source)])
    assert failed["check"]["passed"] is False
    respx.post(URL).mock(return_value=reply(edit(source, GOOD)))
    staged = await server.puppet_write("grow", [str(source)])
    await server.puppet_apply(str(git_repo), staged["patch_id"])
    respx.post(URL).mock(
        return_value=reply(edit(source, GOOD + "\n\ndef mul(a, b):\n    return a * b\n"))
    )
    other = await server.puppet_write("grow again", [str(source)])
    await server.puppet_reject(str(git_repo), other["patch_id"], "no")

    rows = sample_rows(git_repo)
    assert [(r["tool"], r["verdict"]) for r in rows] == [
        ("puppet_write", "check_failed"),
        ("puppet_write", "staged"),
        ("puppet_apply", "applied"),
        ("puppet_write", "staged"),
        ("puppet_reject", "rejected"),
    ]  # fmt: skip
    assert rows[1]["patch_id"] == staged["patch_id"] == rows[2]["patch_id"]
    assert rows[4]["patch_id"] == other["patch_id"]


async def test_nothing_is_written_when_samples_are_off(git_repo, source):
    (git_repo / "puppet-master.toml").write_text(
        (git_repo / "puppet-master.toml").read_text() + "[log]\nsamples = false\n"
    )
    respx.post(URL).mock(return_value=reply({"answer": "a", "claims": []}))
    await server.puppet_read([str(source)], "q")
    assert not (git_repo / ".puppet-master" / "samples").exists()


# --- the size cap --------------------------------------------------------------------------


def test_the_cap_deletes_the_oldest_file_first(tmp_path, monkeypatch):
    folder = tmp_path / ".puppet-master" / "samples"
    folder.mkdir(parents=True)
    (folder / "2020-01-01.jsonl").write_text("a" * 300)
    (folder / "2020-01-02.jsonl").write_text("b" * 300)
    monkeypatch.setattr(samples, "MAX_BYTES", 600)
    samples.record(
        tmp_path,
        Config(),
        tool="puppet_read",
        system="s",
        user="u",
        raw_output="r",
        verdict="verified",
    )
    names = sorted(p.name for p in folder.iterdir())
    assert "2020-01-01.jsonl" not in names
    assert "2020-01-02.jsonl" in names
    assert len(names) == 2


def test_the_cap_never_deletes_the_file_just_written(tmp_path, monkeypatch):
    monkeypatch.setattr(samples, "MAX_BYTES", 1)
    samples.record(
        tmp_path,
        Config(),
        tool="puppet_read",
        system="s",
        user="u",
        raw_output="r",
        verdict="verified",
    )
    assert len(sample_rows(tmp_path)) == 1


# --- reasoning reserve ---------------------------------------------------------------------


async def test_reserve_shrinks_chunks_and_grows_max_tokens(tmp_path):
    path = tmp_path / "big.txt"
    path.write_text("\n".join("x" * 60 for _ in range(40)))  # about 700 tokens rendered
    cfg = replace(Config(), chunk_tokens=1000)
    route = respx.post(URL).mock(return_value=reply({"answer": "a", "claims": [], "claim_ids": []}))

    plain = await puppet_read_impl(cfg, [str(path)], "q")
    assert plain["chunks"] == 1
    assert json.loads(route.calls[0].request.content)["max_tokens"] == READ_MAX_TOKENS

    reserved = replace(cfg, reasoning_reserve=500)
    out = await puppet_read_impl(reserved, [str(path)], "q")
    assert out["chunks"] == 2  # effective chunk size is now 500
    sent = [json.loads(c.request.content)["max_tokens"] for c in route.calls[1:]]
    assert sent == [READ_MAX_TOKENS + 500] * 3  # two map calls and the merge


# --- reasoning tokens in the log -----------------------------------------------------------


async def test_reasoning_tokens_are_logged_and_summarized(git_repo, source):
    respx.post(URL).mock(
        return_value=reply(
            {"answer": "a", "claims": []},
            prompt_tokens=100,
            completion_tokens=50,
            completion_tokens_details={"reasoning_tokens": 40},
        )
    )
    await server.puppet_read([str(source)], "q")
    rows = stats.load_rows(git_repo / ".puppet-master" / "log.jsonl")
    assert rows[0]["reasoning_tokens"] == 40
    assert stats.summarize(rows)["avg_reasoning_tokens"] == {"puppet_read": 40}


async def test_samples_keep_the_thinking_in_raw_output(git_repo, source):
    body = {
        "choices": [
            {"message": {"content": '{"answer": "a", "claims": []}', "reasoning_content": "hmm"}}
        ],
        "usage": {},
    }
    respx.post(URL).mock(return_value=httpx.Response(200, json=body))
    await server.puppet_read([str(source)], "q")
    assert sample_rows(git_repo)[0]["raw_output"].startswith("<think>hmm</think>")


async def test_a_bad_flint_toml_after_apply_does_not_hide_the_result(git_repo, source):
    respx.post(URL).mock(return_value=reply(edit(source, GOOD)))
    staged = await server.puppet_write("grow", [str(source)])
    (git_repo / "puppet-master.toml").write_text("log = 5\n")
    out = await server.puppet_apply(str(git_repo), staged["patch_id"])
    assert out["applied"] is True

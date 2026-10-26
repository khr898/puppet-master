import json
import sys
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
import respx

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
import hallucinations as hx  # noqa: E402

from puppet_master import server  # noqa: E402
from puppet_master.config import Config  # noqa: E402
from puppet_master.read import (  # noqa: E402
    SYSTEM_MERGE,
    Usage,
    chunk_files,
    number_lines,
    puppet_read_impl,
)
from puppet_master.report import est_tokens  # noqa: E402

CFG = Config()
URL = f"{CFG.base_url}/v1/chat/completions"


def llm_reply(content: str | dict) -> httpx.Response:
    text = content if isinstance(content, str) else json.dumps(content)
    body = {
        "choices": [{"message": {"content": text}}],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 50},
    }
    return httpx.Response(200, json=body)


def answer(claims: list, text: str = "the answer") -> dict:
    return {"answer": text, "claims": claims}


def sent_user_message(route, call: int = 0) -> str:
    return json.loads(route.calls[call].request.content)["messages"][1]["content"]


@pytest.fixture
def shapes(tmp_path) -> Path:
    path = tmp_path / "shapes.py"
    path.write_text(hx.SOURCE)
    return path


def retarget(raw: dict, path: Path) -> dict:
    """Point a fixture claim at the real temp file (keeps the 'wrong file' claim wrong)."""
    return {**raw, "file": str(path)} if raw.get("file") == hx.PATH else raw


@respx.mock
async def test_hallucinated_claims_are_stripped_and_good_ones_kept(shapes):
    claims = [retarget(c, shapes) for _, c in hx.BAD + hx.GOOD]
    respx.post(URL).mock(return_value=llm_reply(answer(claims)))
    out = await puppet_read_impl(CFG, [str(shapes)], "what does Circle do?")
    assert len(out["claims"]) == len(hx.GOOD)
    assert out["stripped"] == len(hx.BAD)
    assert out["answer_confidence"] == "degraded"
    assert out["chunks"] == 1
    assert all(set(c) == {"text", "file", "line"} for c in out["claims"])


@respx.mock
async def test_clean_answer_is_not_degraded(shapes):
    good = retarget(hx.GOOD[0][1], shapes)
    respx.post(URL).mock(return_value=llm_reply(answer([good], "Circle.area uses pi")))
    out = await puppet_read_impl(CFG, [str(shapes)], "q")
    assert out == {
        "answer": "Circle.area uses pi",
        "claims": [{"text": "t", "file": str(shapes), "line": 10}],
        "stripped": 0,
        "chunks": 1,
    }


@respx.mock
async def test_invalid_json_twice_is_unverifiable(shapes):
    route = respx.post(URL).mock(return_value=llm_reply("not json"))
    out = await puppet_read_impl(CFG, [str(shapes)], "q")
    assert out == {"error": "unverifiable_output"}
    assert route.call_count == 2


@respx.mock
async def test_json_without_an_answer_counts_as_invalid(shapes):
    route = respx.post(URL).mock(return_value=llm_reply({"claims": []}))
    out = await puppet_read_impl(CFG, [str(shapes)], "q")
    assert out == {"error": "unverifiable_output"}
    assert route.call_count == 2


@respx.mock
async def test_invalid_json_once_then_valid_succeeds_and_asks_for_json_only(shapes):
    route = respx.post(URL).mock(side_effect=[llm_reply("oops"), llm_reply(answer([], "ok"))])
    usage = Usage()
    out = await puppet_read_impl(CFG, [str(shapes)], "q", usage=usage)
    assert out["answer"] == "ok"
    assert sent_user_message(route, 1).endswith(
        "Your last reply was not valid JSON. Reply with JSON only."
    )
    assert usage.retries == 1
    assert usage.tokens_in == 2000 and usage.tokens_out == 100


@respx.mock
async def test_line_range_sends_only_those_lines_with_original_numbers(shapes):
    route = respx.post(URL).mock(
        return_value=llm_reply(
            answer([{"text": "t", "file": str(shapes), "line": 15, "quote": "class Rect"}])
        )
    )
    # lines 10-20 of the fixture; line 15 is blank, the Rect class is on line 16
    out = await puppet_read_impl(CFG, [f"{shapes}:10-20"], "q")
    sent = sent_user_message(route)
    assert "   10|         return math.pi * self.radius ** 2" in sent
    assert "   20|" in sent
    assert "    9|" not in sent and "   21|" not in sent
    assert out["stripped"] == 0 and len(out["claims"]) == 1  # 'class Rect' is within ±2 of 15


@respx.mock
async def test_prompt_puts_files_first_in_sorted_order_and_question_last(tmp_path):
    (tmp_path / "b.py").write_text("B\n")
    (tmp_path / "a.py").write_text("A\n")
    route = respx.post(URL).mock(return_value=llm_reply(answer([])))
    await puppet_read_impl(CFG, [str(tmp_path / "b.py"), str(tmp_path / "a.py")], "why?")
    sent = sent_user_message(route)
    assert sent == (
        f"=== FILE {tmp_path / 'a.py'} ===\n    1| A\n"
        f"=== FILE {tmp_path / 'b.py'} ===\n    1| B\n"
        "\nQUESTION: why?"
    )


@respx.mock
async def test_request_uses_the_read_schema_and_1500_max_tokens(shapes):
    route = respx.post(URL).mock(return_value=llm_reply(answer([])))
    await puppet_read_impl(CFG, [str(shapes)], "q")
    sent = json.loads(route.calls[0].request.content)
    assert sent["max_tokens"] == 1500
    schema = sent["response_format"]["json_schema"]["schema"]
    assert schema["required"] == ["answer", "claims"]
    assert schema["properties"]["claims"]["items"]["required"] == ["text", "file", "line", "quote"]


@respx.mock
async def test_directory_relative_and_unreadable_paths_never_call_flint(tmp_path):
    route = respx.post(URL).mock(return_value=llm_reply(answer([])))
    assert (await puppet_read_impl(CFG, [str(tmp_path)], "q"))["error"] == "directory_not_allowed"
    assert (await puppet_read_impl(CFG, ["src/x.py"], "q"))["error"] == "relative_path"
    missing = str(tmp_path / "nope.py")
    assert await puppet_read_impl(CFG, [missing], "q") == {"error": "unreadable", "path": missing}
    binary = tmp_path / "bin.dat"
    binary.write_bytes(b"\xff\xfe\x00")
    assert (await puppet_read_impl(CFG, [str(binary)], "q"))["error"] == "unreadable"
    assert route.call_count == 0


@respx.mock
async def test_bad_line_range_is_reported(shapes):
    route = respx.post(URL).mock(return_value=llm_reply(answer([])))
    for rng in ("0-5", "9-3", "500-600"):
        out = await puppet_read_impl(CFG, [f"{shapes}:{rng}"], "q")
        assert out["error"] == "bad_range"
    assert route.call_count == 0


@respx.mock
async def test_over_budget_makes_no_llm_call(tmp_path):
    big = tmp_path / "big.txt"
    big.write_text("x" * 40 * 1000 + "\n" + "y" * 4000)  # > 10k tokens numbered
    route = respx.post(URL).mock(return_value=llm_reply(answer([])))
    out = await puppet_read_impl(CFG, [str(big)], "q")
    assert out["error"] == "over_budget"
    assert out["limit"] == CFG.chunk_tokens
    assert out["est_tokens"] > CFG.chunk_tokens
    assert "path:start-end" in out["hint"]
    assert route.call_count == 0


@respx.mock
async def test_ready_hook_runs_after_preflight_and_can_veto(shapes, tmp_path):
    calls = []

    async def ready():
        calls.append(1)
        return {"error": "insufficient_memory"}

    route = respx.post(URL).mock(return_value=llm_reply(answer([])))
    assert await puppet_read_impl(CFG, [str(tmp_path)], "q", ready=ready) == {
        "error": "directory_not_allowed",
        "path": str(tmp_path),
    }
    assert calls == []  # a bad request never starts Flint
    out = await puppet_read_impl(CFG, [str(shapes)], "q", ready=ready)
    assert out == {"error": "insufficient_memory"}
    assert calls == [1] and route.call_count == 0


# --- the MCP tool -------------------------------------------------------------------------


@pytest.fixture
def tool_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.setattr(server, "mem_pressure", lambda: 10)


def mock_up():
    respx.get(f"{CFG.base_url}/health").mock(return_value=httpx.Response(200, json={}))
    respx.get(f"{CFG.base_url}/props").mock(
        return_value=httpx.Response(200, json={"default_generation_settings": {"n_ctx": 16384}})
    )


def log_rows(root: Path) -> list[dict]:
    return [json.loads(x) for x in (root / ".puppet-master" / "log.jsonl").read_text().splitlines()]


@respx.mock
async def test_tool_report_fits_500_tokens_even_with_a_huge_answer(tool_env, shapes):
    mock_up()
    claims = [
        {"text": "word " * 20, "file": str(shapes), "line": 10, "quote": "math.pi"}
        for _ in range(200)
    ]
    respx.post(URL).mock(return_value=llm_reply(answer(claims, "long " * 3000)))
    out = await server.puppet_read([str(shapes)], "q")
    assert est_tokens(json.dumps(out)) <= 500
    assert out["truncated"] is True


@respx.mock
async def test_tool_logs_usage_to_the_project_root(tool_env, shapes):
    mock_up()
    bad = retarget(hx.BAD[0][1], shapes)
    respx.post(URL).mock(return_value=llm_reply(answer([bad])))
    out = await server.puppet_read([str(shapes)], "q")
    assert out["stripped"] == 1
    [row] = log_rows(shapes.parent)  # not in a git repo, so the file's parent is the root
    assert row["tool"] == "puppet_read"
    assert row["outcome"] == "ok"
    assert row["stripped"] == 1
    assert row["flint_tokens_in"] == 1000 and row["flint_tokens_out"] == 50
    assert row["report_tokens"] > 0


@respx.mock
async def test_tool_returns_flint_offline(tool_env, shapes):
    mock_up()
    respx.post(URL).mock(side_effect=httpx.ConnectError("refused"))
    out = await server.puppet_read([str(shapes)], "q")
    assert out == {"error": "flint_offline"}
    assert log_rows(shapes.parent)[0]["outcome"] == "offline"


@respx.mock
async def test_tool_over_budget_is_logged_and_does_not_start_flint(tool_env, tmp_path):
    big = tmp_path / "big.txt"
    big.write_text("x" * 60000)
    health = respx.get(f"{CFG.base_url}/health").mock(return_value=httpx.Response(200))
    out = await server.puppet_read([str(big)], "q")
    assert out["error"] == "over_budget"
    assert health.call_count == 0
    assert log_rows(tmp_path)[0]["outcome"] == "over_budget"


async def test_tool_description_states_the_contract():
    tools = {t.name: t for t in await server.mcp.list_tools()}
    desc = tools["puppet_read"].description
    assert "USE THIS FIRST" in desc and "path:start-end" in desc and "≤8 chunks" in desc


@respx.mock
async def test_an_oversized_question_is_over_budget_without_a_call(shapes):
    route = respx.post(URL).mock(return_value=llm_reply(answer([])))
    out = await puppet_read_impl(CFG, [str(shapes)], "why " * 16000)
    assert out["error"] == "over_budget"
    assert out["limit"] == CFG.ctx - 512 - 1500
    assert route.call_count == 0


# --- T6: multi-chunk map-reduce -----------------------------------------------------------

SMALL = replace(CFG, chunk_tokens=150)  # one 300-char line plus its header fills a chunk


def two_files(tmp_path) -> tuple[Path, Path]:
    a, b = tmp_path / "a.py", tmp_path / "b.py"
    a.write_text("alpha " * 50)
    b.write_text("bravo " * 50)
    return a, b


def claim(path: Path, line: int, quote: str, text: str = "t") -> dict:
    return {"text": text, "file": str(path), "line": line, "quote": quote}


def merge_reply(ids: list[str], text: str = "merged") -> httpx.Response:
    return llm_reply({"answer": text, "claim_ids": ids})


def user_of(route, call: int) -> str:
    return sent_user_message(route, call)


def test_chunk_files_packs_small_files_together_in_sorted_order():
    files = {"/b": (1, ["b"]), "/a": (1, ["a"])}
    assert chunk_files(files, 1000) == [{"/a": (1, ["a"]), "/b": (1, ["b"])}]


def test_chunk_files_starts_a_new_chunk_when_the_next_file_does_not_fit():
    big = "x" * 300
    chunks = chunk_files({"/a": (1, [big]), "/b": (1, [big])}, 150)
    assert chunks == [{"/a": (1, [big])}, {"/b": (1, [big])}]


def test_chunk_files_splits_a_long_file_keeping_true_line_numbers():
    lines = [f"line {i}" for i in range(1, 101)]
    chunks = chunk_files({"/a": (1, lines)}, 50)  # 200 chars per chunk
    assert len(chunks) > 2
    expected_start = 1
    for chunk in chunks:
        start, piece = chunk["/a"]
        assert start == expected_start
        assert piece == lines[start - 1 : start - 1 + len(piece)]
        expected_start += len(piece)
    assert expected_start == 101


def test_chunk_files_split_respects_a_slice_start():
    chunks = chunk_files({"/a": (500, ["y" * 100] * 6)}, 100)
    assert [c["/a"][0] for c in chunks] == [500, 503]


def test_chunk_files_keeps_an_empty_file_and_isolates_an_oversized_line():
    assert chunk_files({}, 100) == []
    assert chunk_files({"/a": (1, [])}, 100) == [{"/a": (1, [])}]
    giant = "z" * 1000
    chunks = chunk_files({"/a": (1, ["a", giant, "b"])}, 100)
    assert [c["/a"] for c in chunks] == [(1, ["a"]), (2, [giant]), (3, ["b"])]


@respx.mock
async def test_two_files_need_three_calls_two_map_and_one_merge(tmp_path):
    a, b = two_files(tmp_path)
    route = respx.post(URL).mock(
        side_effect=[
            llm_reply(answer([claim(a, 1, "alpha alpha", "A says alpha")], "from a")),
            llm_reply(answer([claim(b, 1, "bravo bravo", "B says bravo")], "from b")),
            merge_reply(["c1", "c2"], "alpha and bravo"),
        ]
    )
    usage = Usage()
    out = await puppet_read_impl(SMALL, [str(a), str(b)], "what is said?", usage=usage)
    assert route.call_count == 3
    assert out == {
        "answer": "alpha and bravo",
        "claims": [
            {"text": "A says alpha", "file": str(a), "line": 1},
            {"text": "B says bravo", "file": str(b), "line": 1},
        ],
        "stripped": 0,
        "chunks": 2,
    }
    assert usage.tokens_in == 3000 and usage.tokens_out == 150
    merge_system = json.loads(route.calls[2].request.content)["messages"][0]["content"]
    assert merge_system == SYSTEM_MERGE
    merge_user = user_of(route, 2)
    assert "what is said?" in merge_user
    assert "from a" in merge_user and "from b" in merge_user
    assert f"c1: A says alpha ({a}:1)" in merge_user
    assert f"c2: B says bravo ({b}:1)" in merge_user


@respx.mock
async def test_map_calls_send_only_their_own_chunk(tmp_path):
    a, b = two_files(tmp_path)
    route = respx.post(URL).mock(
        side_effect=[llm_reply(answer([])), llm_reply(answer([])), merge_reply([])]
    )
    await puppet_read_impl(SMALL, [str(a), str(b)], "q")
    assert str(a) in user_of(route, 0) and str(b) not in user_of(route, 0)
    assert str(b) in user_of(route, 1) and str(a) not in user_of(route, 1)


@respx.mock
async def test_merge_cannot_introduce_claims_it_was_not_given(tmp_path):
    a, b = two_files(tmp_path)
    respx.post(URL).mock(
        side_effect=[
            llm_reply(answer([claim(a, 1, "alpha alpha")])),
            llm_reply(answer([claim(b, 1, "bravo bravo")])),
            merge_reply(["c99", "c2", "c2", "nonsense"]),
        ]
    )
    out = await puppet_read_impl(SMALL, [str(a), str(b)], "q")
    assert [c["file"] for c in out["claims"]] == [str(b)]  # c99 and nonsense dropped, c2 once


@respx.mock
async def test_stripped_is_summed_across_chunks_and_marks_the_answer_degraded(tmp_path):
    a, b = two_files(tmp_path)
    respx.post(URL).mock(
        side_effect=[
            llm_reply(answer([claim(a, 1, "invented"), claim(a, 1, "alpha alpha")])),
            llm_reply(answer([claim(b, 1, "also invented")])),
            merge_reply(["c1"]),
        ]
    )
    out = await puppet_read_impl(SMALL, [str(a), str(b)], "q")
    assert out["stripped"] == 2
    assert out["answer_confidence"] == "degraded"
    assert len(out["claims"]) == 1


@respx.mock
async def test_unverified_claims_never_reach_the_merge_step(tmp_path):
    a, b = two_files(tmp_path)
    route = respx.post(URL).mock(
        side_effect=[
            llm_reply(answer([claim(a, 1, "invented", "FABRICATED")])),
            llm_reply(answer([])),
            merge_reply([]),
        ]
    )
    await puppet_read_impl(SMALL, [str(a), str(b)], "q")
    assert "FABRICATED" not in user_of(route, 2)


@respx.mock
async def test_merge_invalid_twice_is_unverifiable(tmp_path):
    a, b = two_files(tmp_path)
    route = respx.post(URL).mock(
        side_effect=[
            llm_reply(answer([])),
            llm_reply(answer([])),
            llm_reply("garbage"),
            llm_reply({"answer": "x", "claim_ids": "c1"}),  # wrong type is invalid too
        ]
    )
    out = await puppet_read_impl(SMALL, [str(a), str(b)], "q")
    assert out == {"error": "unverifiable_output"}
    assert route.call_count == 4
    assert user_of(route, 3).endswith("Reply with JSON only.")


@respx.mock
async def test_map_chunk_invalid_twice_stops_before_merge(tmp_path):
    a, b = two_files(tmp_path)
    route = respx.post(URL).mock(return_value=llm_reply("garbage"))
    out = await puppet_read_impl(SMALL, [str(a), str(b)], "q")
    assert out == {"error": "unverifiable_output"}
    assert route.call_count == 2  # both attempts on chunk 1, nothing after


@respx.mock
async def test_big_file_is_split_and_a_claim_verifies_against_the_right_slice(tmp_path):
    big = tmp_path / "big.txt"
    big.write_text("\n".join(f"line number {i} content" + "." * 14 for i in range(1, 2501)))
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        system, user = body["messages"][0]["content"], body["messages"][1]["content"]
        if system == SYSTEM_MERGE:
            return merge_reply(["c1"], "found it")
        seen.append(user)
        # The same claim is made in every chunk; only the chunk holding line 2400 may keep it.
        return llm_reply(answer([claim(big, 2400, "line number 2400 content")]))

    respx.post(URL).mock(side_effect=handler)
    out = await puppet_read_impl(CFG, [str(big)], "where is 2400?")
    assert out["chunks"] == len(seen) == 3
    assert out["stripped"] == 2
    assert out["claims"] == [{"text": "t", "file": str(big), "line": 2400}]
    assert "    1| line number 1 content" in seen[0]
    first_of_second = int(seen[1].split("\n")[1].split("|")[0])
    assert (
        first_of_second > 1 and f"{first_of_second:>5}| line number {first_of_second} " in seen[1]
    )


@respx.mock
async def test_eight_chunks_are_allowed_and_nine_are_over_budget(tmp_path):
    files = []
    for i in range(9):
        f = tmp_path / f"f{i}.txt"
        f.write_text("q" * 300)
        files.append(str(f))
    route = respx.post(URL).mock(
        side_effect=[llm_reply(answer([])) for _ in range(8)] + [merge_reply([])]
    )
    out = await puppet_read_impl(SMALL, files[:8], "q")
    assert out["chunks"] == 8 and route.call_count == 9
    route.reset()
    out = await puppet_read_impl(SMALL, files, "q")
    assert out["error"] == "over_budget"
    assert out["limit"] == SMALL.chunk_tokens * SMALL.max_chunks
    assert route.call_count == 0


@respx.mock
async def test_ready_hook_runs_once_for_a_multi_chunk_read(tmp_path):
    a, b = two_files(tmp_path)
    respx.post(URL).mock(
        side_effect=[llm_reply(answer([])), llm_reply(answer([])), merge_reply([])]
    )
    calls = []

    async def ready():
        calls.append(1)

    await puppet_read_impl(SMALL, [str(a), str(b)], "q", ready=ready)
    assert calls == [1]


@respx.mock
async def test_no_files_is_an_error_without_a_call():
    route = respx.post(URL).mock(return_value=llm_reply(answer([])))
    assert await puppet_read_impl(CFG, [], "q") == {"error": "no_files"}
    assert route.call_count == 0


@respx.mock
async def test_merge_prompt_too_large_for_the_window_is_over_budget(tmp_path):
    a, b = two_files(tmp_path)
    huge = "word " * 9000
    route = respx.post(URL).mock(
        side_effect=[llm_reply(answer([], huge)), llm_reply(answer([], huge))]
    )
    out = await puppet_read_impl(SMALL, [str(a), str(b)], "q")
    assert out["error"] == "over_budget"
    assert route.call_count == 2  # the merge call itself is never sent


def _filler_module(path: Path, n: int = 60) -> None:
    """A module of n unrelated functions, about 15 tokens each, plus one the question names."""
    funcs = [f"def helper_{i}(value):\n    return value * {i} + {i}\n" for i in range(n)]
    funcs.insert(n // 2, "def retry_backoff(attempt):\n    return BACKOFF_BASE ** attempt\n")
    path.write_text("BACKOFF_BASE = 2\n\n\n" + "\n\n".join(funcs))


@respx.mock
async def test_focus_shows_only_the_matching_blocks_and_their_definitions(tmp_path):
    mod = tmp_path / "mod.py"
    _filler_module(mod)
    cfg = replace(CFG, chunk_tokens=300, focus_tokens=100)
    lines = mod.read_text().splitlines()
    line = lines.index("    return BACKOFF_BASE ** attempt") + 1
    claim = {"text": "t", "file": str(mod), "line": line, "quote": "BACKOFF_BASE ** attempt"}
    route = respx.post(URL).mock(return_value=llm_reply(answer([claim])))

    out = await puppet_read_impl(cfg, [str(mod)], "how does retry_backoff grow?", focus=True)

    msg = sent_user_message(route)
    assert route.call_count == 1  # one focused chunk, no map-reduce
    assert "def retry_backoff(attempt):" in msg
    assert "BACKOFF_BASE = 2" in msg  # the constant it uses came along
    assert "helper_5" not in msg
    assert "(lines not shown)" in msg
    assert out["claims"] == [{"text": "t", "file": str(mod), "line": line}]
    assert out["focus"]["blocks"] < out["focus"]["of_blocks"]
    assert out["focus"]["shown_tokens"] < out["focus"]["total_tokens"]


@respx.mock
async def test_focus_strips_a_claim_on_a_line_flint_was_not_shown(tmp_path):
    mod = tmp_path / "mod.py"
    _filler_module(mod)
    cfg = replace(CFG, chunk_tokens=300, focus_tokens=100)
    hidden = mod.read_text().splitlines().index("def helper_5(value):") + 1
    claim = {"text": "t", "file": str(mod), "line": hidden, "quote": "def helper_5(value):"}
    respx.post(URL).mock(return_value=llm_reply(answer([claim])))
    out = await puppet_read_impl(cfg, [str(mod)], "how does retry_backoff grow?", focus=True)
    assert out["claims"] == [] and out["stripped"] == 1


@respx.mock
async def test_focus_lifts_the_chunk_limit_but_exhaustive_keeps_it(tmp_path):
    mod = tmp_path / "mod.py"
    _filler_module(mod, n=400)
    cfg = replace(CFG, chunk_tokens=300, max_chunks=2, focus_tokens=60)
    respx.post(URL).mock(return_value=llm_reply(answer([])))
    assert (await puppet_read_impl(cfg, [str(mod)], "retry_backoff"))["error"] == "over_budget"
    assert "focus" in await puppet_read_impl(cfg, [str(mod)], "retry_backoff", focus=True)


@respx.mock
async def test_focus_with_no_matching_term_falls_back_to_the_normal_limits(tmp_path):
    mod = tmp_path / "mod.py"
    _filler_module(mod, n=400)
    cfg = replace(CFG, chunk_tokens=300, max_chunks=2, focus_tokens=60)
    route = respx.post(URL).mock(return_value=llm_reply(answer([])))
    out = await puppet_read_impl(cfg, [str(mod)], "zzz qqq", focus=True)
    assert out["error"] == "over_budget" and route.call_count == 0


def test_number_lines_marks_each_gap_once():
    assert (
        number_lines(["a", None, None, "d"], start=3)
        == "    3| a\n  ...| (lines not shown)\n    6| d"
    )


def test_prompts_ask_for_the_values_themselves_not_a_reference_to_them():
    from puppet_master.read import SYSTEM_READ

    for prompt in (SYSTEM_READ, SYSTEM_MERGE):
        assert "State the names, values and numbers themselves" in prompt

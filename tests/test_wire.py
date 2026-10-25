import json

from flint_bridge import server
from flint_bridge.report import factor_base


async def test_tools_send_one_compact_json_text_block(tmp_path):
    out = await server.mcp.call_tool("flint_check", {"cmd": "test", "root": "relative/dir"})
    blocks = out[0] if isinstance(out, tuple) else out
    assert len(blocks) == 1
    text = blocks[0].text
    assert json.loads(text) == {"error": "relative_path", "path": "relative/dir"}
    assert text == '{"error":"relative_path","path":"relative/dir"}'


async def test_tools_have_no_structured_output_to_duplicate_the_text():
    for tool in await server.mcp.list_tools():
        assert tool.outputSchema is None, tool.name


def test_claims_in_one_folder_share_a_base():
    out = factor_base(
        {
            "answer": "a",
            "claims": [
                {"text": "x", "file": "/repo/src/pkg/a.py", "line": 1, "quote": "q"},
                {"text": "y", "file": "/repo/src/pkg/sub/b.py", "line": 2, "quote": "q"},
            ],
        }
    )
    assert out["base"] == "/repo/src/pkg"
    assert [c["file"] for c in out["claims"]] == ["a.py", "sub/b.py"]


def test_a_single_claim_keeps_its_full_path():
    result = {"claims": [{"text": "x", "file": "/repo/a.py", "line": 1, "quote": "q"}]}
    assert factor_base(result) == result


def test_reports_without_claims_are_unchanged():
    assert factor_base({"error": "over_budget"}) == {"error": "over_budget"}

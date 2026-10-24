import re
from pathlib import Path

from flint_bridge import server
from flint_bridge.config import load_config

ROOT = Path(__file__).parent.parent

# Each tool's description must carry its limits and its mandatory-check wording.
REQUIRED = {
    "flint_status": ["Call once per session"],
    "flint_server": ["~/.flint/server.toml", "before compiling", "Use stop"],
    "flint_read": ["USE THIS FIRST", "≤10k tokens", "≤8 chunks", "read its cited lines yourself"],
    "flint_write": ["STAGED", "MUST read the patch", "≤3 files", "≤6k tokens", "max 2 attempts"],
    "flint_apply": ["read the patch file yourself", "Refuses if checks failed"],
    "flint_reject": ["failed attempt"],
    "flint_check": ["allow-listed", "only the failures", "patch_id"],
    "flint_ask": ["UNVERIFIED", "flint_read"],
}


async def test_every_tool_has_its_limit_and_check_wording():
    tools = {t.name: t.description for t in await server.mcp.list_tools()}
    assert set(tools) == set(REQUIRED)
    for name, phrases in REQUIRED.items():
        for phrase in phrases:
            assert phrase in tools[name], f"{name} description lacks {phrase!r}"


def test_instructions_state_the_budgets_and_mandatory_checks():
    for phrase in [
        "≤10k tokens per chunk",
        "≤8 chunks",
        "≤3 files",
        "≤6k tokens",
        "Checks are mandatory",
    ]:
        assert phrase in server.INSTRUCTIONS


def test_project_flint_toml_has_test_and_lint_checks():
    cfg = load_config(ROOT)
    assert cfg.check_default == "test"
    assert cfg.check_commands["test"] == ["uv", "run", "pytest", "-q", "-x"]
    assert cfg.check_commands["lint"] == ["uv", "run", "ruff", "check", "."]


def test_claude_md_check_commands_match_flint_toml():
    text = (ROOT / "CLAUDE.md").read_text()
    assert "## Flint delegation" in text
    assert "docs/spec/SPEC.md" in text
    assert "UV_CACHE_DIR=.uv-cache" in text
    for name, argv in load_config(ROOT).check_commands.items():
        assert re.search(rf"^- {name}: `{re.escape(' '.join(argv))}`$", text, re.M), name

"""Smoke tests against a real llama-server on the configured port: `uv run pytest -m live`."""

import asyncio
import json
import os
from pathlib import Path

import pytest

from flint_bridge import llm, server
from flint_bridge.config import Config
from flint_bridge.report import est_tokens

pytestmark = pytest.mark.live

REPO = Path(__file__).parent.parent.parent


@pytest.fixture(autouse=True)
def flint_running():
    if not asyncio.run(llm.health(Config()))["up"]:
        pytest.skip("llama-server is not reachable (start it with flint_server)")


def assert_fits_report_cap(result: dict) -> None:
    assert est_tokens(json.dumps(result)) <= 500


async def test_status_reports_up():
    out = await server.flint_status()
    assert out["up"] is True
    assert {"ctx", "budgets", "managed"} <= set(out)
    assert_fits_report_cap(out)


async def test_read_answers_with_verified_claims():
    path = REPO / "src" / "flint_bridge" / "report.py"
    out = await server.flint_read([str(path)], "what does cap() protect from removal?")
    assert "error" not in out, out
    assert isinstance(out["answer"], str) and out["answer"]
    assert {"claims", "stripped", "chunks"} <= set(out)
    base = out.get("base", "")
    assert all(os.path.join(base, c["file"]) == str(path) for c in out["claims"])
    assert_fits_report_cap(out)


async def test_ask_returns_an_unverified_draft():
    out = await server.flint_ask("Write a one-line commit message for fixing a typo in a README.")
    assert out["verified"] is False
    assert out["draft"]
    assert_fits_report_cap(out)


async def test_write_stages_a_docstring_without_touching_the_tree(git_repo):
    app = git_repo / "app.py"
    before = app.read_text()
    out = await server.flint_write("Add a one-line docstring to add().", [str(app)])
    assert "error" not in out, out
    assert {"patch_id", "patch_path", "diffstat", "check"} <= set(out)
    assert Path(out["patch_path"]).exists()
    assert app.read_text() == before
    assert_fits_report_cap(out)

import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from flint_bridge import gitops, server
from flint_bridge.config import Config
from flint_bridge.gitops import baseline_worktree, run_git, stage_and_check

PYTEST = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
CFG = replace(Config(), check_commands={"test": PYTEST})

ADD = "def add(a, b):\n    return a + b\n"
APP_WITH_LOCAL_EDIT = "# local edit\n" + ADD


def git(root: Path, *args: str) -> str:
    return run_git(root, *args)


def status(root: Path) -> str:
    return git(root, "status", "--porcelain")


def worktrees(root: Path) -> list[str]:
    return git(root, "worktree", "list").splitlines()


@pytest.fixture
def repo(tmp_path) -> Path:
    """One commit, one uncommitted edit (app.py), one untracked file (notes.txt)."""
    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    (root / ".gitignore").write_text(".venv/\n.flint/\n__pycache__/\n")
    (root / "app.py").write_text(ADD)
    (root / "test_app.py").write_text(
        "from app import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    (root / "app.py").write_text(APP_WITH_LOCAL_EDIT)
    (root / "notes.txt").write_text("scratch\n")
    (root / ".venv").mkdir()
    (root / ".venv" / "marker").write_text("x")
    return root


def test_baseline_recreates_commit_edit_and_untracked_file(repo):
    before = status(repo)
    with baseline_worktree(repo) as tmp:
        assert (tmp / "app.py").read_text() == APP_WITH_LOCAL_EDIT
        assert (tmp / "notes.txt").read_text() == "scratch\n"
        assert (tmp / "test_app.py").exists()
        # the index is the user's current state, so nothing is pending yet
        assert git(tmp, "diff") == ""
        assert status(repo) == before
    assert status(repo) == before
    assert len(worktrees(repo)) == 1
    assert not tmp.exists()


def test_venv_is_symlinked_not_copied(repo):
    with baseline_worktree(repo) as tmp:
        assert (tmp / ".venv").is_symlink()
        assert (tmp / ".venv").resolve() == (repo / ".venv").resolve()
    assert (repo / ".venv" / "marker").exists()  # removing the worktree spares the target


def test_stage_and_check_reports_a_breaking_change_and_leaves_the_tree_alone(repo):
    before = status(repo)
    broken = APP_WITH_LOCAL_EDIT.replace("a + b", "a - b")
    out = stage_and_check(CFG, repo, {repo / "app.py": broken}, "test")
    assert out["check"]["passed"] is False
    assert any("test_add" in f for f in out["check"]["failures"])
    assert "-    return a + b" in out["patch"] and "+    return a - b" in out["patch"]
    changed = [
        ln for ln in out["patch"].splitlines() if ln[:1] in "+-" and ln[:3] not in ("+++", "---")
    ]
    assert not any("local edit" in ln for ln in changed)  # the user's own edit is baseline
    assert "app.py" in out["diffstat"]
    assert status(repo) == before
    assert (repo / "app.py").read_text() == APP_WITH_LOCAL_EDIT
    assert len(worktrees(repo)) == 1


def test_patch_applies_to_the_real_tree(repo):
    changed = APP_WITH_LOCAL_EDIT + "\n\ndef sub(a, b):\n    return a - b\n"
    out = stage_and_check(CFG, repo, {repo / "app.py": changed}, "test")
    assert out["check"]["passed"] is True
    git_apply = subprocess.run(
        ["git", "apply", "--check"], cwd=repo, input=out["patch"], text=True, capture_output=True
    )
    assert git_apply.returncode == 0, git_apply.stderr


def test_new_file_shows_up_in_the_patch(repo):
    out = stage_and_check(CFG, repo, {repo / "pkg" / "new_mod.py": "X = 1\n"}, "test")
    assert "new file mode" in out["patch"]
    assert "+X = 1" in out["patch"]
    assert not (repo / "pkg").exists()


def test_identical_content_is_no_changes(repo):
    out = stage_and_check(CFG, repo, {repo / "app.py": APP_WITH_LOCAL_EDIT}, "test")
    assert out == {"error": "no_changes"}


def test_paths_outside_the_repo_are_refused(repo, tmp_path):
    for target in (tmp_path / "evil.py", repo / ".." / "evil.py"):
        out = stage_and_check(CFG, repo, {target: "x = 1\n"}, "test")
        assert out["error"] == "path_outside_root"
    assert not (tmp_path / "evil.py").exists()


def test_unlisted_check_name_is_refused_before_any_worktree_is_made(repo):
    out = stage_and_check(CFG, repo, {repo / "app.py": "x = 1\n"}, "rm -rf /")
    assert out == {"error": "not_allowed", "allowed": ["test"]}
    assert len(worktrees(repo)) == 1


def test_worktree_is_removed_when_the_check_raises(repo, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("check exploded")

    monkeypatch.setattr(gitops, "run_check", boom)
    with pytest.raises(RuntimeError):
        stage_and_check(CFG, repo, {repo / "app.py": "x = 1\n"}, "test")
    assert len(worktrees(repo)) == 1


def allow_test_check(repo: Path) -> None:
    argv = ", ".join(f'"{a}"' for a in PYTEST)
    (repo / "flint.toml").write_text(f"[check.commands]\ntest = [{argv}]\n")


def stage(repo: Path, patch_id: str, patch: str) -> None:
    d = repo / ".flint" / "staging"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{patch_id}.patch").write_text(patch)


async def test_flint_check_with_patch_id_runs_against_the_staged_patch(repo):
    allow_test_check(repo)
    bad = stage_and_check(
        CFG, repo, {repo / "app.py": APP_WITH_LOCAL_EDIT.replace("+", "-")}, "test"
    )
    stage(repo, "aaa111", bad["patch"])
    good_src = APP_WITH_LOCAL_EDIT + "\nY = 2\n"
    stage(repo, "bbb222", stage_and_check(CFG, repo, {repo / "app.py": good_src}, "test")["patch"])
    before = status(repo)

    failing = await server.flint_check("test", str(repo), "aaa111")
    passing = await server.flint_check("test", str(repo), "bbb222")

    assert failing["passed"] is False and failing["failures"]
    assert passing["passed"] is True
    assert status(repo) == before
    assert len(worktrees(repo)) == 1


async def test_flint_check_with_bad_patch_ids(repo):
    allow_test_check(repo)
    assert (await server.flint_check("test", str(repo), "dead99"))["error"] == "unknown_patch"
    assert (await server.flint_check("test", str(repo), "../../etc"))["error"] == "invalid_patch_id"
    stage(repo, "ccc333", "this is not a patch\n")
    assert (await server.flint_check("test", str(repo), "ccc333"))["error"] == "does_not_apply"

import subprocess
import sys
from pathlib import Path

import pytest

from puppet_master.gitops import run_git

PYTEST_ARGV = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
ADD = "def add(a, b):\n    return a + b\n"


@pytest.fixture
def git_repo(tmp_path) -> Path:
    """A committed project: app.py, test_app.py, and a puppet-master.toml allowing 'test'."""
    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    run_git(root, "config", "user.email", "t@example.com")
    run_git(root, "config", "user.name", "t")
    argv = ", ".join(f'"{a}"' for a in PYTEST_ARGV)
    (root / "puppet-master.toml").write_text(
        f'[check]\ndefault = "test"\n[check.commands]\ntest = [{argv}]\n'
    )
    (root / ".gitignore").write_text(".venv/\n.puppet-master/\n__pycache__/\n")
    (root / "app.py").write_text(ADD)
    (root / "test_app.py").write_text(
        "from app import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture(autouse=True)
def _home(tmp_path_factory, monkeypatch):
    """Keep ~/.puppet-master (signing key, server files) out of the real home directory."""
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))

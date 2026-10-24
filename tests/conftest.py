import subprocess
import sys
from pathlib import Path

import pytest

from flint_bridge.gitops import run_git

PYTEST_ARGV = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
ADD = "def add(a, b):\n    return a + b\n"


@pytest.fixture
def git_repo(tmp_path) -> Path:
    """A committed project: app.py + test_app.py, and a flint.toml allowing the 'test' check."""
    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    run_git(root, "config", "user.email", "t@example.com")
    run_git(root, "config", "user.name", "t")
    argv = ", ".join(f'"{a}"' for a in PYTEST_ARGV)
    (root / "flint.toml").write_text(
        f'[check]\ndefault = "test"\n[check.commands]\ntest = [{argv}]\n'
    )
    (root / ".gitignore").write_text(".venv/\n.flint/\n__pycache__/\n")
    (root / "app.py").write_text(ADD)
    (root / "test_app.py").write_text(
        "from app import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", "init")
    return root

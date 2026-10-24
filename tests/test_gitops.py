import subprocess

from flint_bridge.gitops import toplevel


def test_toplevel_of_a_subfolder_is_the_repo_root(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    assert toplevel(sub).resolve() == tmp_path.resolve()


def test_toplevel_outside_a_repo_is_none(tmp_path):
    assert toplevel(tmp_path) is None

from pathlib import Path

import pytest

from puppet_master.budget import PathError, expand, fits, parse_path
from puppet_master.config import Config


def test_parse_path_plain():
    assert parse_path("/a/b.py") == (Path("/a/b.py"), None, None)


def test_parse_path_with_range():
    assert parse_path("/a/b.py:10-20") == (Path("/a/b.py"), 10, 20)


@pytest.mark.parametrize("p", ["/a/b.py:10", "/a/b.py:x-y", "/a/b.py:10-", "/a:1-2/b.py"])
def test_parse_path_only_treats_a_trailing_numeric_range_as_a_range(p):
    path, start, end = parse_path(p)
    assert (start, end) == (None, None)
    assert str(path) == p


def test_fits_leaves_512_tokens_of_headroom():
    cfg = Config()  # ctx 16384
    assert fits(14000, 1500, cfg)
    assert fits(16384 - 512 - 1500, 1500, cfg)
    assert not fits(16384 - 512 - 1500 + 1, 1500, cfg)


def test_expand_returns_literal_files_in_order(tmp_path):
    a, b = tmp_path / "a.py", tmp_path / "b.py"
    a.write_text("x")
    b.write_text("y")
    assert expand([str(b), str(a)]) == [(b, None, None), (a, None, None)]


def test_expand_globs_recursively_and_keeps_the_range(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "m.py").write_text("x")
    (tmp_path / "top.py").write_text("x")
    out = expand([str(tmp_path / "**" / "*.py")])
    assert sorted(p for p, _, _ in out) == [tmp_path / "pkg" / "m.py", tmp_path / "top.py"]
    ranged = expand([str(tmp_path / "top.py") + ":2-4"])
    assert ranged == [(tmp_path / "top.py", 2, 4)]


def test_expand_keeps_a_missing_literal_path_so_the_read_can_report_it(tmp_path):
    missing = tmp_path / "gone.py"
    assert expand([str(missing)]) == [(missing, None, None)]


def test_expand_rejects_a_directory(tmp_path):
    with pytest.raises(PathError) as e:
        expand([str(tmp_path)])
    assert e.value.result["error"] == "directory_not_allowed"


def test_expand_rejects_a_glob_that_matches_a_directory(tmp_path):
    (tmp_path / "sub").mkdir()
    with pytest.raises(PathError) as e:
        expand([str(tmp_path / "s*")])
    assert e.value.result["error"] == "directory_not_allowed"


def test_expand_rejects_a_relative_path():
    with pytest.raises(PathError) as e:
        expand(["src/x.py"])
    assert e.value.result == {"error": "relative_path", "path": "src/x.py"}

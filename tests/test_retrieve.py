from flint_bridge.retrieve import bm25, rank, split_blocks, terms, with_definitions


def test_terms_split_identifiers_and_drop_stopwords():
    out = terms("parseHTTPResponse snake_case_name the")
    for t in ["parsehttpresponse", "parse", "http", "response", "snake_case_name", "snake", "case"]:
        assert t in out
    assert "the" not in out


def test_blocks_start_at_unindented_lines_after_a_blank_and_keep_decorators():
    lines = ["import os", "", "def a():", "    return 1", "", "", "@dec", "def b():", "    pass"]
    blocks = split_blocks("f.py", 1, lines)
    assert [b.start for b in blocks] == [1, 3, 7]
    assert blocks[2].lines[:2] == ["@dec", "def b():"]
    assert [b.end for b in blocks] == [2, 6, 9]


def test_closing_brackets_do_not_start_a_block():
    lines = ["X = [", "    1,", "", "]", "", "y = 2"]
    assert [b.start for b in split_blocks("f.py", 1, lines)] == [1, 6]


def test_long_blocks_are_cut_with_true_line_numbers():
    lines = ["def f():", *[f"    x{i} = {i}" for i in range(4)]]
    blocks = split_blocks("f.py", 10, lines, max_lines=2)
    assert [(b.start, len(b.lines)) for b in blocks] == [(10, 2), (12, 2), (14, 1)]


def test_bm25_prefers_the_doc_with_the_term_and_is_zero_for_no_query():
    docs = [["return", "other"], ["clamp", "return"]]
    a, b = bm25("clamp", docs)
    assert b > a == 0
    assert bm25("the of", docs) == [0.0, 0.0]


def test_rank_puts_the_matching_block_first():
    lines = ["def other():", "    return 2", "", "def clamp(x, lo, hi):", "    return x"]
    best = rank("where is the clamp helper", split_blocks("f.py", 1, lines))[0]
    assert best[1].start == 4 and best[0] > 0


def test_a_top_block_brings_the_definitions_it_uses_from_its_file():
    lines = ["LIMIT = 5", "", "def f():", "    return LIMIT", "", "def g():", "    pass"]
    limit, f, g = split_blocks("f.py", 1, lines)
    assert with_definitions([f, g], [limit, f, g]) == [f, limit, g]
    elsewhere = split_blocks("other.py", 1, ["LIMIT = 9"])
    assert with_definitions([f], [*elsewhere, f]) == [f]

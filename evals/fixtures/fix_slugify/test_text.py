from text import slugify


def test_slugify_joins_words_with_hyphens():
    assert slugify("Hello World") == "hello-world"


def test_slugify_collapses_extra_spaces():
    assert slugify("  Many   spaces here ") == "many-spaces-here"

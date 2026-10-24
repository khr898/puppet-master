"""Canned "hallucinating Flint" claims against one small source file."""

PATH = "/repo/src/shapes.py"

SOURCE = '''\
"""Small geometry helpers."""
import math


class Circle:
    def __init__(self, radius):
        self.radius = radius

    def area(self):
        return math.pi * self.radius ** 2

    def circumference(self):
        return 2 * math.pi * self.radius


class Rect:
    def __init__(self, w, h):
        self.w = w
        self.h = h

    def area(self):
        return self.w * self.h

    def perimeter(self):
        return 2 * (self.w + self.h)


def largest(shapes):
    best = None
    for shape in shapes:
        if best is None or shape.area() > best.area():
            best = shape
    return best


def total_area(shapes):
    return sum(s.area() for s in shapes)


def describe(shape):
    return f"{type(shape).__name__} with area {shape.area():.2f}"
'''

LINES = SOURCE.splitlines()
FILES = {PATH: LINES}


def _claim(**over):
    base = {"text": "t", "file": PATH, "line": 9, "quote": "return math.pi * self.radius ** 2"}
    return {**base, **over}


# (id, raw claim dict). The real text is on line 10 of SOURCE.
BAD = [
    ("made_up_function", _claim(line=10, quote="def volume(self):")),
    ("wrong_file", _claim(file="/repo/src/other.py", line=10)),
    ("line_off_by_5", _claim(line=15)),
    ("paraphrased_quote", _claim(line=10, quote="returns pi times radius squared")),
    ("empty_quote", _claim(quote="")),
    ("whitespace_only_quote", _claim(quote="   \t ")),
    ("line_zero", _claim(line=0)),
    ("line_past_eof", _claim(line=len(LINES) + 1)),
    (
        "quote_spans_beyond_window",
        _claim(
            line=10,
            quote="return math.pi * self.radius ** 2 def circumference(self): "
            "return 2 * math.pi * self.radius",
        ),
    ),
    ("non_numeric_line", _claim(line="ten")),
    ("missing_quote_key", {"text": "t", "file": PATH, "line": 10}),
]

GOOD = [
    ("exact_match", _claim(line=10)),
    ("extra_whitespace", _claim(line=10, quote="return   math.pi *\tself.radius  ** 2")),
    ("line_plus_two", _claim(line=8, quote="return math.pi * self.radius ** 2")),
    ("line_minus_two", _claim(line=12, quote="return math.pi * self.radius ** 2")),
]

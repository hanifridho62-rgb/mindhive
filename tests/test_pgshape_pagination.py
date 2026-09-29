# SPDX-License-Identifier: Apache-2.0
"""Strict pagination and field trimming in the shaper (blueprint P5, Pillar 3).

Spec §10.8.2 and runbook step 4 (§10.11) ask for
`paginate_records(records, page=1, page_size=20)` and
`trim_field_content(val, max_chars=500)` with three constants. The binding plan
decision (Pilar 3 row) puts them in `pgshape` -- the spec puts them in
`pgtools` -- re-exported by `pgtools` by identity, and keeps
`MAX_ROWS_CEILING` at 500: the §10.4 figure of 200 is not adopted.

The envelope is the spec's, key for key and in the spec's order. Two cases the
spec leaves to its own formula are pinned as that formula answers them: an
EMPTY input has `total_pages == 0` (not 1), and a page past the end echoes the
page asked for, with no items and the true totals.

Two cases are pinned by `pgshape`'s own contract rather than by the spec's
code. The module docstring says nothing here raises -- an exception escaping
into an MCP handler is an outage inside the agent's turn -- while §10.8.2's
`item.items()` raises on a row that is not a dict and `max(1, page)` raises on
a page that is not a number. Both degrade here instead.

The new names are reached as `pgshape.<name>` inside each test, so a missing
name fails the tests that need it one by one rather than as one collection
error.
"""
import pytest

from mindhive import pgshape, pgtools

# --- fixture builders -------------------------------------------------------

MARK = "... [TRUNCATED: {} chars hidden]"      # §10.8.2, verbatim

ENVELOPE_KEYS = ["page", "page_size", "total_items", "total_pages",
                 "has_next", "has_prev", "items"]


def _rows(n):
    """*n* distinct short rows; `i` makes every slice checkable by position."""
    return [{"i": i, "text": f"row {i}"} for i in range(n)]


def _pages(total, size):
    return -(-total // size)                      # ceiling division


# --- the constants ----------------------------------------------------------

def test_the_three_page_constants_are_the_specs():
    assert pgshape.DEFAULT_PAGE_SIZE == 20
    assert pgshape.MAX_PAGE_SIZE_CEILING == 100
    assert pgshape.MAX_FIELD_CHARS == 500


def test_the_row_ceiling_stays_500_not_the_200_of_section_10_4():
    """REGRESSION PIN (passes before the feature exists). The plan resolved
    §10.4's `MAX_ROWS_CEILING = 200` against the repo's 500 in the repo's
    favour; `query_database` and `get_current_state` bound `max_rows` by it."""
    assert pgshape.MAX_ROWS_CEILING == 500
    assert pgtools.MAX_ROWS_CEILING is pgshape.MAX_ROWS_CEILING


@pytest.mark.parametrize("name", ["DEFAULT_PAGE_SIZE", "MAX_PAGE_SIZE_CEILING",
                                  "MAX_FIELD_CHARS", "paginate_records",
                                  "trim_field_content"])
def test_each_new_name_is_re_exported_by_pgtools_as_the_same_object(name):
    """`tests/test_pgshape.py` derives its re-export check from `vars(pgshape)`
    and so will cover these once they exist; this names them now, so the
    re-export is demanded before the helper is written rather than after."""
    assert getattr(pgtools, name) is getattr(pgshape, name)


# --- paginate_records: the envelope -----------------------------------------

def test_the_envelope_carries_exactly_the_seven_spec_keys_in_the_spec_order():
    env = pgshape.paginate_records(_rows(3))
    assert list(env) == ENVELOPE_KEYS


def test_the_default_page_is_the_first_twenty_of_forty_five_with_three_pages():
    """45 rather than a round number: ceiling division says 3 pages, floor
    division says 2, and 40 or 60 would let both be right."""
    records = _rows(45)
    assert pgshape.paginate_records(records) == {
        "page": 1, "page_size": 20, "total_items": 45, "total_pages": 3,
        "has_next": True, "has_prev": False, "items": records[:20]}


@pytest.mark.parametrize("page,start,stop,has_next", [
    (2, 20, 40, True), (3, 40, 45, False)])
def test_a_middle_page_and_the_short_last_page(page, start, stop, has_next):
    records = _rows(45)
    env = pgshape.paginate_records(records, page=page)
    assert env["items"] == records[start:stop]
    assert env["has_next"] is has_next and env["has_prev"] is True
    assert (env["page"], env["total_pages"], env["total_items"]) == (page, 3, 45)


def test_an_exact_multiple_has_no_phantom_last_page():
    """Kills `total // size + 1`, which agrees with ceiling division on every
    total that is NOT a multiple of the page."""
    records = _rows(40)
    env = pgshape.paginate_records(records, page=2)
    assert env["total_pages"] == 2
    assert env["items"] == records[20:40]
    assert env["has_next"] is False


@pytest.mark.parametrize("page", [4, 10**9])
def test_a_page_past_the_end_is_empty_and_keeps_the_true_totals(page):
    assert pgshape.paginate_records(_rows(45), page=page) == {
        "page": page, "page_size": 20, "total_items": 45, "total_pages": 3,
        "has_next": False, "has_prev": True, "items": []}


@pytest.mark.parametrize("page", [0, -5])
def test_a_page_below_one_is_the_first_page(page):
    records = _rows(45)
    env = pgshape.paginate_records(records, page=page)
    assert env["page"] == 1
    assert env["items"] == records[:20]
    assert env["has_prev"] is False


@pytest.mark.parametrize("asked,used", [
    (0, 1), (-1, 1), (1, 1), (99, 99), (100, 100), (101, 100), (10**9, 100)])
def test_page_size_is_clamped_into_one_to_one_hundred(asked, used):
    """150 rows, so a size of 101 or 10**9 that escaped the ceiling would show
    as more than 100 items, and a size of 0 that escaped the floor would show
    as none."""
    records = _rows(150)
    env = pgshape.paginate_records(records, page_size=asked)
    assert env["page_size"] == used
    assert env["items"] == records[:used]
    assert env["total_pages"] == _pages(150, used)


def test_an_empty_input_has_zero_pages_and_no_neighbours():
    """The spec's own formula, `(0 + size - 1) // size`, answers 0 -- pinned as
    it answers, rather than as the `max(1, ...)` a UI might prefer."""
    assert pgshape.paginate_records([]) == {
        "page": 1, "page_size": 20, "total_items": 0, "total_pages": 0,
        "has_next": False, "has_prev": False, "items": []}


# --- paginate_records: trimming inside the page -----------------------------

def test_long_string_fields_are_trimmed_inside_the_page_and_nothing_else_is():
    nested = ["y" * 600]
    record = {"text": "x" * 600, "n": 7, "tags": nested,
              "meta": {"k": "z" * 600}, "none": None, "edge": "e" * 500}

    (item,) = pgshape.paginate_records([record])["items"]

    assert item == {"text": "x" * 500 + MARK.format(100), "n": 7,
                    "tags": nested, "meta": {"k": "z" * 600}, "none": None,
                    "edge": "e" * 500}
    assert item["tags"] is nested        # a non-string is handed back, not walked


def test_paginating_never_mutates_the_rows_it_was_given():
    """The dashboard hands this function rows that live in a process-global
    memo. Trimming them IN PLACE would cut the cached fold itself, and every
    later unpaged poll would serve the cut text as if it were the ledger's."""
    record = {"text": "x" * 600}
    records = [record]

    env = pgshape.paginate_records(records)

    assert record == {"text": "x" * 600}
    assert records == [record]
    assert env["items"][0]["text"] == "x" * 500 + MARK.format(100)


# --- trim_field_content -----------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("a" * 501, "a" * 500 + MARK.format(1)),   # one over the cap: cut
    ("a" * 500, "a" * 500),                     # exactly the cap: kept
    ("é" * 501, "é" * 500 + MARK.format(1)),    # characters, not UTF-8 bytes
    ("", ""),
], ids=["one-over-the-cap", "exactly-the-cap", "characters-not-bytes", "empty"])
def test_trim_field_content_cuts_a_string_longer_than_500_characters(value,
                                                                     expected):
    assert pgshape.trim_field_content(value) == expected


def test_trim_field_content_honours_an_explicit_max_chars():
    assert pgshape.trim_field_content("abcdef", max_chars=4) == \
        "abcd" + MARK.format(2)
    assert pgshape.trim_field_content("abcd", max_chars=4) == "abcd"


@pytest.mark.parametrize("value", [
    12345, 3.5, None, True, b"b" * 600, ["s" * 600], {"k": "v" * 600}],
    ids=["int", "float", "None", "bool", "bytes", "list", "dict"])
def test_trim_field_content_hands_back_a_non_string_untouched(value):
    assert pgshape.trim_field_content(value) is value


# --- totality: `pgshape` never raises ---------------------------------------

def test_a_row_that_is_not_a_dict_is_trimmed_as_a_value_not_raised_on():
    rows = ["s" * 501, 7, None, ["t" * 600]]
    env = pgshape.paginate_records(rows)
    assert env["items"] == ["s" * 500 + MARK.format(1), 7, None, ["t" * 600]]
    assert env["total_items"] == 4


def test_a_page_argument_that_is_absent_takes_the_default():
    """None is the caller declining to say -- `dashboard_query._coerced`'s
    rule for an absent value -- so it means exactly the defaults."""
    records = _rows(45)
    env = pgshape.paginate_records(records, page=None, page_size=None)
    assert (env["page"], env["page_size"]) == (1, 20)
    assert env["items"] == records[:20]


@pytest.mark.parametrize("bad", ["2", 2.5, True, object()])
def test_a_page_argument_that_is_not_an_int_still_yields_a_consistent_envelope(
        bad):
    """No fallback VALUE is pinned for these -- only that the answer is an
    envelope a caller can trust: integer page and size inside their bounds, and
    items that are exactly that page. `True` is the discriminating row: it is an
    `int` subclass, so an `isinstance(x, int)` guard lets it through, and a size
    taken as given from there goes out as `"page_size": true`. (The spec's own
    `max(1, min(True, 100))` happens to answer the int 1, which passes.)"""
    records = _rows(45)
    env = pgshape.paginate_records(records, page=bad, page_size=bad)
    page, size = env["page"], env["page_size"]
    assert type(page) is int and page >= 1
    assert type(size) is int and 1 <= size <= 100
    assert env["items"] == records[(page - 1) * size:page * size]
    assert env["total_pages"] == _pages(45, size)

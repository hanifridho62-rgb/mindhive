from mindhive import dedupe


# --- normalize ---------------------------------------------------------------

def test_normalize_lowercases_and_strips_punctuation():
    assert dedupe.normalize("Use the BROWSER, please!") == frozenset({"please", "browser"})


def test_normalize_drops_stopwords_and_empty():
    assert dedupe.normalize("the and of to") == frozenset()
    assert dedupe.normalize("") == frozenset()
    assert dedupe.normalize(None) == frozenset()


# --- similar -----------------------------------------------------------------

def test_similar_true_for_rewording_above_threshold():
    a = dedupe.normalize("prefer the browser for visual content like mockups and diagrams")
    b = dedupe.normalize("prefer browser for visual content like mockups and diagrams")
    assert dedupe.similar(a, b) is True


def test_similar_false_for_distinct_topics():
    a = dedupe.normalize("rebuild the exe with python 3.12 before launching")
    b = dedupe.normalize("prefer the browser for visual mockups and diagrams")
    assert dedupe.similar(a, b) is False


def test_similar_threshold_boundary():
    # 4 shared of 5 union = 0.8 < 0.85 default → not similar
    a = frozenset({"w", "x", "y", "z"})
    b = frozenset({"w", "x", "y", "q"})
    assert dedupe.similar(a, b) is False          # jaccard 3/5 = 0.6
    assert dedupe.similar(a, a) is True            # identical
    assert dedupe.similar(a, b, threshold=0.5) is True


def test_similar_empty_sets_not_similar():
    assert dedupe.similar(frozenset(), frozenset()) is False


# --- collapse ----------------------------------------------------------------

def _m(text, typ="caveat", project="p", ts="t"):
    return {"text": text, "type": typ, "project": project, "ts": ts}


def test_collapse_keeps_newest_as_representative():
    items = [
        _m("prefer browser for visual content like mockups and diagrams", ts="2"),  # newest
        _m("prefer the browser for visual content like mockups and diagrams", ts="1"),
    ]
    out = dedupe.collapse(items)
    assert len(out) == 1
    assert out[0]["ts"] == "2"               # newest wins
    assert out[0]["similar_count"] == 1


def test_collapse_isolated_by_type():
    items = [
        _m("same exact wording here friend", typ="caveat"),
        _m("same exact wording here friend", typ="decision"),
    ]
    out = dedupe.collapse(items)
    assert len(out) == 2                      # different type → not merged


def test_collapse_isolated_by_project():
    items = [
        _m("same exact wording here friend", project="a"),
        _m("same exact wording here friend", project="b"),
    ]
    out = dedupe.collapse(items)
    assert len(out) == 2                      # different project → not merged


def test_collapse_keeps_distinct_memos():
    items = [
        _m("rebuild the exe with python 3.12 before launching"),
        _m("prefer browser for visual mockups and diagrams"),
    ]
    out = dedupe.collapse(items)
    assert len(out) == 2
    assert all(m["similar_count"] == 0 for m in out)


def test_collapse_preserves_full_text():
    long = "x " * 500 + "tail"
    out = dedupe.collapse([_m(long)])
    assert out[0]["text"] == long             # nothing truncated


def test_collapse_does_not_mutate_input():
    items = [_m("hello world friend")]
    dedupe.collapse(items)
    assert "similar_count" not in items[0]


def test_collapse_blank_memos_do_not_merge():
    out = dedupe.collapse([_m("   "), _m("")])
    assert len(out) == 2                      # empties never collapse together

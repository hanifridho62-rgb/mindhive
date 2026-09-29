# tests/test_theme_contrast.py — blueprint K12 (Bug 12): the palette, measured.
"""The dashboard palette against WCAG 2.x contrast, computed rather than claimed.

Three documents have quoted a ratio for the furniture grey `#6b7488` over the
header surface `#171a23`: the spec says 3.87:1, the MEB says 3.2:1. Measured
with the WCAG 2.x formula it is 3.71:1 -- neither -- which is why every figure
in this file is COMPUTED by `theme.contrast_ratio` and asserted against a value
worked out here, independently, from the published formula:

    per channel  c = v / 255;  c / 12.92 if c <= 0.03928
                               else ((c + 0.055) / 1.055) ** 2.4
    luminance    L = 0.2126 R + 0.7152 G + 0.0722 B
    ratio        (L_lighter + 0.05) / (L_darker + 0.05)

AA asks 4.5:1 of normal text and 3.0:1 of non-text marks (WCAG 1.4.3, 1.4.11).

K12 (binding plan decision) moves three tokens to the MEB values -- `bg`
#12151c, `text` #e2e8f0, `dim` #9aa3b8 -- adds the MEB's `border`, `success`,
`warning` and `danger` to the `blueprint` group beside the `accent` an earlier
phase landed, and exposes the MEB's flat dict verbatim as
`theme.BLUEPRINT_PALETTE`. `ok`/`warn`/`bad` are NOT replaced: every document
the dashboard serves still shares those three.

`dimmest` was the one text token still under AA after K12 -- #555c6e, 2.60:1 on
`--surface` -- and it is brightened rather than exempted (plan line 222: every
text token clears 4.5:1). Brightened as the SAME grey: its hue and saturation
at a higher lightness, the darkest such value that clears 4.5:1 on the ground
it is drawn on (`--inset`, read from the sheet) and on `--surface` and `--bg`,
and still quieter than `faint`.
"""
import colorsys
import re

import pytest

from mindhive import dashboard_css, dashboard_page, taskview, theme

# MEB §10 (THEME-01), verbatim and in the MEB's order.
MEB_PALETTE = {
    "bg": "#12151c",
    "surface": "#171a23",
    "border": "#282d3d",
    "text": "#e2e8f0",
    "dim": "#9aa3b8",
    "accent": "#3182ce",
    "success": "#38a169",
    "warning": "#dd6b20",
    "danger": "#e53e3e",
}

AA_TEXT = 4.5
AA_NON_TEXT = 3.0


def _shell_page() -> str:
    return dashboard_page.render_page("test-token", tasks_css=taskview.CARDS_CSS,
                                      tasks_js=taskview.CARDS_JS)


def _rule(selector: str) -> str:
    """The declarations of the one rule in the page's own sheet written for
    exactly *selector*, whitespace removed."""
    bodies = re.findall(r"(?:^|[\s}])" + re.escape(selector) + r"\s*\{([^}]*)\}",
                        dashboard_css.CSS)
    assert len(bodies) == 1, f"{selector!r} heads {len(bodies)} rules, not one"
    return re.sub(r"\s+", "", bodies[0])


# --- the WCAG helpers ----------------------------------------------------------

def test_black_on_white_is_the_largest_ratio_there_is():
    assert theme.contrast_ratio("#000000", "#ffffff") == pytest.approx(21.0)


def test_a_colour_against_itself_is_the_smallest_ratio_there_is():
    for colour in ("#171a23", "#9aa3b8", "#ffffff", "#000000"):
        assert theme.contrast_ratio(colour, colour) == pytest.approx(1.0)


def test_the_ratio_is_the_same_whichever_colour_is_named_first():
    """Lighter over darker, not first over second: a helper that divided in
    argument order would report 0.15 for dark text on a light ground."""
    forward = theme.contrast_ratio("#9aa3b8", "#171a23")
    assert forward == pytest.approx(theme.contrast_ratio("#171a23", "#9aa3b8"))
    assert forward > 1.0


def test_luminance_weights_the_three_channels_as_wcag_does():
    assert theme.relative_luminance("#ff0000") == pytest.approx(0.2126)
    assert theme.relative_luminance("#00ff00") == pytest.approx(0.7152)
    assert theme.relative_luminance("#0000ff") == pytest.approx(0.0722)
    assert theme.relative_luminance("#ffffff") == pytest.approx(1.0)
    assert theme.relative_luminance("#000000") == pytest.approx(0.0)


def test_a_mid_grey_goes_through_the_gamma_curve():
    """The independent mid value. A helper that skipped linearisation would
    call #808080 half as bright as white (0.502) instead of 0.216."""
    expected = ((128 / 255 + 0.055) / 1.055) ** 2.4          # worked out here
    assert theme.relative_luminance("#808080") == pytest.approx(expected)
    assert theme.contrast_ratio("#808080", "#ffffff") == \
        pytest.approx(1.05 / (expected + 0.05))
    assert round(theme.contrast_ratio("#808080", "#ffffff"), 2) == 3.95


def test_a_near_black_channel_takes_the_linear_branch():
    assert theme.relative_luminance("#050505") == pytest.approx((5 / 255) / 12.92)


def test_the_published_aa_boundary_greys_fall_either_side_of_4_5():
    """#767676 is the lightest grey that passes AA on white and #777777 the
    darkest that fails -- the pair every contrast checker is calibrated on."""
    assert round(theme.contrast_ratio("#767676", "#ffffff"), 2) == 4.54
    assert round(theme.contrast_ratio("#777777", "#ffffff"), 2) == 4.48


def test_shorthand_and_upper_case_name_the_same_colour():
    """The palette writes `#fff`, `#2a5`, `#3d6` and `#888`; a helper that
    only read six digits would misjudge exactly those tokens."""
    white = theme.relative_luminance("#fff")
    assert white == pytest.approx(theme.relative_luminance("#ffffff"))
    assert white == pytest.approx(theme.relative_luminance("#FFFFFF"))
    assert theme.relative_luminance("#2a5") == \
        pytest.approx(theme.relative_luminance("#22aa55"))


@pytest.mark.parametrize("bad", ["#12345", "#1234", "#1234abcd", "#ggg", "",
                                 None, 0xffffff, b"#ffffff"])
def test_a_value_that_is_not_a_three_or_six_digit_hex_colour_is_refused(bad):
    """`#12345` would otherwise parse as r=0x12, g=0x34, b=0x5 and return a
    confident, wrong ratio; an alpha channel needs compositing no ratio has.
    A value that is not a string at all -- `None`, the int `0xffffff`, bytes --
    is refused the same way, by the documented ValueError, rather than by
    whichever AttributeError or TypeError the parse happens to hit first."""
    with pytest.raises(ValueError):
        theme.relative_luminance(bad)


# --- K12: the values -----------------------------------------------------------

def test_k12_moves_bg_text_and_dim_to_the_meb_values():
    t = theme.tokens()
    assert (t["bg"], t["text"], t["dim"]) == ("#12151c", "#e2e8f0", "#9aa3b8")


def test_the_header_surface_keeps_its_value():
    """REGRESSION PIN: `surface` is the one MEB value the palette already had."""
    assert theme.tokens()["surface"] == "#171a23"


def test_the_three_shared_status_colours_stay():
    """REGRESSION PIN: K12 adds success/warning/danger BESIDE these; it does
    not replace them. Every served document shares these three."""
    assert {name: value for name, (value, _why) in theme.PALETTE["status"].items()} \
        == {"ok": "#46c46a", "warn": "#e0b341", "bad": "#ff8a8a"}


def test_blueprint_palette_is_the_meb_dict_verbatim():
    assert theme.BLUEPRINT_PALETTE == MEB_PALETTE
    assert list(theme.BLUEPRINT_PALETTE) == list(MEB_PALETTE), \
        "same pairs, different order: the MEB's order is part of 'verbatim'"


def test_blueprint_palette_is_what_the_theme_resolves_for_those_names():
    """One set of values, two views of it: the flat MEB dict must never say a
    colour the page's own palette does not."""
    resolved = theme.tokens()
    assert {name: resolved.get(name) for name in MEB_PALETTE} \
        == dict(theme.BLUEPRINT_PALETTE)


def test_the_meb_tokens_new_to_the_palette_live_in_the_blueprint_group():
    """`bg`, `surface`, `text` and `dim` already had homes; the other five are
    the MEB's own. A duplicate of an old name in this group would silently win
    `tokens()` (later groups overwrite earlier ones)."""
    assert set(theme.PALETTE["blueprint"]) == \
        {"border", "accent", "success", "warning", "danger"}
    for name, (_value, why) in theme.PALETTE["blueprint"].items():
        assert why.strip(), f"--{name} states no purpose"


def test_the_served_page_declares_every_meb_token_with_its_meb_value():
    page = _shell_page()
    missing = [f"--{name}: {value};" for name, value in MEB_PALETTE.items()
               if f"--{name}: {value};" not in page]
    assert not missing, f"the page's :root does not declare {missing}"


# --- K12: the ratios -----------------------------------------------------------

@pytest.mark.parametrize("fg,ground", [
    ("text", "surface"), ("text", "bg"),
    ("dim", "surface"), ("dim", "bg"),
    # The nav's group headings are `dim` on the RAIL: the spec names them.
    ("dim", "rail"),
])
def test_body_text_and_furniture_text_meet_aa_for_normal_text(fg, ground):
    t = theme.tokens()
    ratio = theme.contrast_ratio(t[fg], t[ground])
    assert ratio >= AA_TEXT, \
        f"--{fg} {t[fg]} on --{ground} {t[ground]} is {ratio:.2f}:1, under {AA_TEXT}"


@pytest.mark.parametrize("fg", ["accent", "success", "warning", "danger"])
def test_the_meb_signal_colours_meet_aa_for_non_text_on_the_surface(fg):
    t = theme.tokens()
    ratio = theme.contrast_ratio(t[fg], t["surface"])
    assert ratio >= AA_NON_TEXT, \
        f"--{fg} {t[fg]} on --surface is {ratio:.2f}:1, under {AA_NON_TEXT}"


# Every ratio a document quotes (theme.py, TODOS #7, the session legend), with
# the figure it quotes. The thresholds above let a retune pass while every one
# of these figures goes stale -- `dim` at #a0a8bc still clears 4.5:1 -- so each
# is pinned to the two places it is quoted to. The 3.71 of the old grey is
# pinned by its canary below, 2.60 by the sweep's, 1.25 by the quietest-text
# test. `#a4b0c6` is not a token: it is the spec's comparison grey, quoted
# with its measured ratio beside the spec's 8.2.
@pytest.mark.parametrize("fg,ground,quoted", [
    ("dim", "surface", 6.87), ("dim", "bg", 7.22),
    ("dimmest", "surface", 4.51), ("dimmest", "inset", 4.60),
    ("#a4b0c6", "surface", 7.95),
])
def test_every_ratio_the_docs_quote_is_the_measured_one(fg, ground, quoted):
    t = theme.tokens()
    measured = theme.contrast_ratio(t.get(fg, fg), t[ground])
    assert round(measured, 2) == quoted, \
        f"{fg} on --{ground} measures {measured:.4f}:1; the docs quote {quoted}"


def _under_aa(palette: dict, ground: str) -> dict:
    """{text token: its ratio, 2 places} for every `text`-group token of
    *palette* that is under AA on the colour *ground*."""
    return {name: round(theme.contrast_ratio(palette[name], ground), 2)
            for name in theme.PALETTE["text"]
            if theme.contrast_ratio(palette[name], ground) < AA_TEXT}


@pytest.mark.parametrize("ground", ["surface", "bg"])
def test_every_text_token_meets_aa_on_the_header_and_the_page(ground):
    """No exemptions: plan line 222 holds EVERY text token to 4.5:1."""
    t = theme.tokens()
    assert _under_aa(t, t[ground]) == {}, f"text tokens under AA on --{ground}"


def test_the_sweep_would_catch_the_grey_the_skipped_step_used_to_be():
    """CANARY for the sweep above: today's palette with `dimmest` put back to
    #555c6e must be reported, at its real ratio, and nothing else with it."""
    t = theme.tokens()
    assert _under_aa(dict(t, dimmest="#555c6e"), t["surface"]) == {"dimmest": 2.60}


def test_the_skipped_step_glyph_meets_aa_on_the_ground_it_is_drawn_on():
    """`dimmest` is drawn in ONE place, the glyph of a skipped roadmap step, and
    the node paints its own ground under the glyph -- the row's hover tint never
    reaches it. The ground is read from the sheet rather than assumed, and a
    second use of the token fails the count until its ground is checked here."""
    assert _shell_page().count("var(--dimmest)") == 1
    assert "color:var(--dimmest)" in _rule(".rmnode.rmskip")
    ground = re.search(r"(?:^|;)background:var\(--([\w-]+)\)", _rule(".rmnode")).group(1)
    assert ground == "inset"
    t = theme.tokens()
    ratio = theme.contrast_ratio(t["dimmest"], t[ground])
    assert ratio >= AA_TEXT, f"--dimmest on --{ground} is {ratio:.2f}:1"


def _hls(hex_colour: str) -> tuple:
    r, g, b = (int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return colorsys.rgb_to_hls(r, g, b)


def test_the_skipped_step_grey_is_brightened_along_its_own_hue():
    """Brightened, not repainted: the hue and saturation of #555c6e, to within
    what 8-bit channels allow, at a higher lightness -- measured with `colorsys`,
    independently of the theme. 223.2 deg / 12.8% / 38.2% lightness becomes
    222.0 deg / 12.6% / 53.3%."""
    new = theme.tokens()["dimmest"]
    assert new == "#798297"
    (h0, l0, s0), (h1, l1, s1) = _hls("#555c6e"), _hls(new)
    assert abs(h1 - h0) * 360 < 1.5
    assert abs(s1 - s0) * 100 < 0.5
    assert l1 > l0


def test_the_skipped_step_is_still_the_quietest_text_on_the_board():
    """Brighter, still last: a skipped step has to read quieter than the pending
    step beside it (`faint`), or the stepper loses a distinction it draws."""
    t = theme.tokens()
    quietest = min(theme.PALETTE["text"], key=lambda n: theme.relative_luminance(t[n]))
    assert quietest == "dimmest"
    assert round(theme.contrast_ratio(t["faint"], t["dimmest"]), 2) == 1.25


# --- K12: where the MEB tokens land ------------------------------------------
# The K12 row names the element each new token draws: the usage bar `--accent`,
# the initiative pill `--border`, the red CI card `--danger`, the breaker badge
# `--warning`/`--success`. Each lands on the rule that element wears; the
# renderer that puts the class on the element is read from the served script;
# and each edge is measured against the ground the sheet gives that element,
# because a border is a non-text mark (WCAG 1.4.11, 3.0:1).

def _page_function(name: str) -> str:
    """`function <name>(...){...}` out of the page's one script, by braces."""
    page = _shell_page()
    script = page[page.index("<script>"):page.index("</script>")]
    starts = [m.start() for m in re.finditer(rf"\bfunction {name}\(", script)]
    assert len(starts) == 1, f"{name}: {len(starts)} definitions in the page"
    depth = 0
    for j in range(script.index("{", starts[0]), len(script)):
        depth += {"{": 1, "}": -1}.get(script[j], 0)
        if depth == 0:
            return script[starts[0]:j + 1]
    raise AssertionError(f"{name}: unbalanced braces")


def _ground(selector: str) -> str:
    """The token the one rule for *selector* that declares a background uses."""
    grounds = [m for body in re.findall(
        r"(?:^|[\s}])" + re.escape(selector) + r"\s*\{([^}]*)\}", dashboard_css.CSS)
        for m in re.findall(r"(?:^|;)background:var\(--([\w-]+)\)",
                            re.sub(r"\s+", "", body))]
    assert len(grounds) == 1, f"{selector!r} declares {len(grounds)} grounds"
    return grounds[0]


def test_the_initiative_pill_is_edged_with_the_meb_border():
    """It carried a literal `#8b6fe0` edge, the drift K12 exists to remove."""
    rule = _rule(".pill.wsinit")
    assert "border-color:var(--border)" in rule
    assert not re.search(r"border-color:#", rule), "a literal edge is back"


def test_a_card_a_ci_check_failed_is_edged_with_the_meb_danger():
    """The card `ovStatus` marks `sci` -- a failed run with a CI verdict -- and
    only that one: a plain failure keeps the ordinary card."""
    assert "border-color:var(--danger)" in _rule(".hits li.ovci")
    card = _page_function("ovCard")
    assert "(st[1] === 'sci' ? ' class=\"ovci\"' : '')" in card
    assert card.count("ovci") == 1
    t = theme.tokens()
    ground = _ground(".hits li")
    ratio = theme.contrast_ratio(t["danger"], t[ground])
    assert ratio >= AA_NON_TEXT, f"--danger on --{ground} is {ratio:.2f}:1"


def test_a_tripped_breaker_badge_is_edged_with_the_meb_warning():
    """The badge is the card's status pill, which reads `tripped` exactly when
    `ovStatus` marks the run `strip` (a `breaker_open` run)."""
    assert "border-color:var(--warning)" in _rule(".pill.ovtrip")
    card = _page_function("ovCard")
    assert "(st[1] === 'strip' ? ' ovtrip' : '')" in card
    assert card.count("ovtrip") == 1
    t = theme.tokens()
    ground = _ground(".pill")
    ratio = theme.contrast_ratio(t["warning"], t[ground])
    assert ratio >= AA_NON_TEXT, f"--warning on --{ground} is {ratio:.2f}:1"


def test_the_status_classes_the_card_keys_on_are_the_ones_ovstatus_gives():
    """CANARY for the two tests above: `sci` and `strip` are what `ovStatus`
    answers for a CI failure and a tripped breaker, so a rename there cannot
    leave the card keyed on a class nothing produces."""
    status = _page_function("ovStatus")
    assert "r.ci ? 'sci' : 'sbad'" in status
    assert "'breaker_open') return ['\\u26a1', 'strip', 'tripped']" in status


def test_the_old_furniture_grey_fails_aa_on_the_header_surface():
    """CANARY for the threshold tests: the value K12 replaces must fail the
    same check the new one passes, or the check proves nothing. The spec
    quotes 3.87:1 and the MEB 3.2:1; measured, it is 3.71:1."""
    ratio = theme.contrast_ratio("#6b7488", "#171a23")
    assert round(ratio, 2) == 3.71
    assert ratio < AA_TEXT

# tests/test_dashboard_nav_split.py — blueprint P5 Bug 13: sections and actions, two landmarks.
"""The rail used to be one `<nav>` holding two different kinds of button.

Sixteen of them take the reader to a section; ten of them DO something --
export, sync, embed, the self-test -- and all twenty-six sat in one column under
one landmark, told apart only by the absence of `data-sec` and a quieter face
keyed on `nav button:not([data-sec])`. Bug 13 splits them: `<nav
aria-label="sections">` keeps only the section buttons (`data-sec`,
`class="nav-link"`), and a sibling `<div id="actions" role="group"
aria-label="actions">` holds the commands (`class="nav-action"`, no `data-sec`).

What must NOT change is pinned first, from a snapshot of the page taken before
the split: every section id with the handler it calls, and every action
handler. A split that dropped one button, or moved one to the wrong side, fails
here by name.

The markup is read with the stdlib `HTMLParser`, which treats `<script>` and
`<style>` bodies as opaque text, so the `<button` strings the page's renderers
build at runtime are never mistaken for the rail's own markup.

The second half of spec 2.13 -- a late response for the previously selected
project must not paint over the new one -- is ALREADY enforced, in `apiGet`
(`dashboard_js_core.py`), and executed against the live page by the tier-7
cases of `tests/test_paint_guard_node.py`. The default-suite pin here asserts
its ordering, so `hive selftest` (which deselects the node tier) still sees it.
"""
import re
from html.parser import HTMLParser

from mindhive import dashboard_page, taskview

# The RED snapshot: the page before the split, section id -> its onclick.
SECTIONS = {
    "overview": "showOverview()",
    "oversee": "showOversee()",
    "memory": "showMemory()",
    "sessions": "showSessions()",
    "improvements": "showImprovements(true)",
    "log": "showLog()",
    "sops": "showSops()",
    "state": "showState()",
    "briefing": "showBriefing()",
    "recall": "showRecall()",
    "graph": "showGraph()",
    "skills": "showSkillPanel()",
    "ws-parallel": "showWorkspace('parallel')",
    "ws-swarm": "showWorkspace('swarm')",
    "ws-research": "showWorkspace('research')",
    "append": "showAppend()",
}
# ... and every command, in rail order.
ACTIONS = (
    "doExport()",
    "doAction('/api/project','Project (rebuild)')",
    "doAction('/api/sync','Sync')",
    "runJob('/api/harvest','Catching up')",
    "doAction('/api/verify','Verify chain', 'GET')",
    "runEmbed()",
    "confirmJob('/api/plugins/scan','Plugins scan')",
    "runReflect()",
    "runResolve()",
    "confirmJob('/api/selftest','Self-test')",
)


def _page(swarm=True) -> str:
    return dashboard_page.render_page("tok", tasks_css=taskview.CARDS_CSS,
                                      tasks_js=taskview.CARDS_JS, swarm=swarm)


# --- a small element tree ------------------------------------------------------

class _Node:
    __slots__ = ("tag", "attrs", "parent", "children", "text")

    def __init__(self, tag, attrs, parent):
        self.tag, self.attrs, self.parent = tag, attrs, parent
        self.children, self.text = [], []

    def classes(self) -> set:
        return set((self.attrs.get("class") or "").split())

    def walk(self):
        for child in self.children:
            yield child
            yield from child.walk()

    def ancestors(self):
        node = self.parent
        while node is not None:
            yield node
            node = node.parent

    def all_text(self) -> str:
        return "".join(self.text) + "".join(c.all_text() for c in self.children)


class _Tree(HTMLParser):
    """Elements with their parents. `<script>`/`<style>` content is data to
    `HTMLParser`, never markup; an end tag closes the nearest open match."""
    VOID = frozenset(("area", "base", "br", "col", "embed", "hr", "img", "input",
                      "link", "meta", "param", "source", "track", "wbr"))

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root", {}, None)
        self._open = [self.root]
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, dict(attrs), self._open[-1])
        self._open[-1].children.append(node)
        if tag not in self.VOID:
            self._open.append(node)

    def handle_startendtag(self, tag, attrs):
        self._open[-1].children.append(_Node(tag, dict(attrs), self._open[-1]))

    def handle_endtag(self, tag):
        for i in range(len(self._open) - 1, 0, -1):
            if self._open[i].tag == tag:
                del self._open[i:]
                return

    def handle_data(self, data):
        self._open[-1].text.append(data)

    def find_all(self, pred) -> list:
        return [n for n in self.root.walk() if pred(n)]


def _navs(tree) -> list:
    return tree.find_all(lambda n: n.tag == "nav")


def _actions(tree) -> list:
    return tree.find_all(lambda n: n.attrs.get("id") == "actions")


def _buttons(node) -> list:
    return [n for n in node.walk() if n.tag == "button"]


def _headings(node) -> list:
    return [n.all_text().strip() for n in node.walk()
            if n.tag == "div" and "group" in n.classes()]


def _unowned_nav_buttons(tree) -> list:
    """The onclick of every nav button that is not a marked section link."""
    return [b.attrs.get("onclick") for nav in _navs(tree) for b in _buttons(nav)
            if "data-sec" not in b.attrs or "nav-link" not in b.classes()]


def _section_map(nav) -> dict:
    return {b.attrs["data-sec"]: b.attrs.get("onclick")
            for b in _buttons(nav) if "data-sec" in b.attrs}


# --- a small stylesheet reader -------------------------------------------------

def _stylesheet(page) -> str:
    assert page.count("<style>") == 1, "the page grew a second <style> block"
    css = page[page.index("<style>") + len("<style>"):page.index("</style>")]
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _declarations(body) -> dict:
    out = {}
    for part in body.split(";"):
        prop, sep, value = part.partition(":")
        if sep and prop.strip():
            out[prop.strip().lower()] = " ".join(value.split())
    return out


def _top_level_rules(css) -> list:
    """[(selectors, declarations)] for every rule OUTSIDE an at-rule: a
    reduced-motion or breakpoint override is not the rule's resting face."""
    rules, i = [], 0
    while True:
        brace = css.find("{", i)
        if brace == -1:
            return rules
        prelude = css[i:brace].strip()
        depth, j = 1, brace + 1
        while j < len(css) and depth:
            depth += {"{": 1, "}": -1}.get(css[j], 0)
            j += 1
        body = css[brace + 1:j - 1]
        if prelude and not prelude.startswith("@") and "{" not in body:
            selectors = tuple(" ".join(s.split()) for s in prelude.split(","))
            rules.append((selectors, _declarations(body)))
        i = j


def _merged(rules, selector) -> dict:
    """Every declaration a top-level rule gives `selector`, later rules winning."""
    out = {}
    for selectors, decls in rules:
        if selector in selectors:
            out.update(decls)
    return out


def _every_selector(css) -> set:
    """Each selector of every innermost rule, at-rule bodies included."""
    return {" ".join(one.split())
            for sel, _body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)
            for one in sel.split(",")}


# --- the markup: two landmarks -------------------------------------------------

def test_the_page_has_exactly_one_nav_and_it_is_the_sections_landmark():
    """REGRESSION PIN (passes before the split): one `<nav>`, labelled."""
    navs = _navs(_Tree(_page()))
    assert len(navs) == 1
    assert navs[0].attrs.get("aria-label") == "sections"


def test_the_actions_live_in_their_own_labelled_group():
    (group,) = _actions(_Tree(_page())) or [None]
    assert group is not None, "no element with id=\"actions\""
    assert (group.tag, group.attrs.get("role"), group.attrs.get("aria-label")) \
        == ("div", "group", "actions")


def test_the_actions_group_sits_outside_the_nav():
    tree = _Tree(_page())
    (group,) = _actions(tree) or [None]
    assert group is not None, "no element with id=\"actions\""
    assert not any(a.tag == "nav" for a in group.ancestors()), \
        "the actions group is still inside the sections landmark"


def test_the_nav_and_the_actions_share_one_rail_column():
    """`main` is a two-column grid. A third direct child would be placed in
    the PANEL's column and push the panel down a row, so the two landmarks
    have to share a parent that is itself the rail's one grid item."""
    tree = _Tree(_page())
    (nav,) = _navs(tree)
    (group,) = _actions(tree) or [None]
    assert group is not None, "no element with id=\"actions\""
    assert group.parent is nav.parent, "the actions group is not the nav's sibling"
    assert nav.parent.tag != "main", \
        "nav and actions are both direct grid items of <main>"
    siblings = nav.parent.children
    assert siblings.index(nav) < siblings.index(group), \
        "the commands sit above the navigation"


def test_every_nav_button_owns_a_section_and_says_so():
    unowned = _unowned_nav_buttons(_Tree(_page()))
    assert not unowned, \
        f"nav buttons without data-sec and class nav-link: {unowned}"


def test_the_nav_button_check_would_catch_an_action_left_behind():
    """CANARY for the test above."""
    snippet = ('<nav aria-label="sections">'
               '<button class="nav-link" data-sec="a" onclick="showA()">A</button>'
               '<button class="nav-link" onclick="doExport()">X</button>'
               '<button data-sec="b" onclick="showB()">B</button></nav>')
    assert _unowned_nav_buttons(_Tree(snippet)) == ["doExport()", "showB()"]


def _command_problems(group) -> list:
    """Every way a button in the commands group fails to be a command."""
    problems = []
    for b in _buttons(group):
        call = b.attrs.get("onclick") or ""
        if "data-sec" in b.attrs:
            problems.append(f"{call} claims a section")
        if "nav-action" not in b.classes():
            problems.append(f"{call} is not a nav-action")
        if not call:
            problems.append("an action button that calls nothing")
        elif call.startswith("show"):
            # Every section handler is a `show*()`, every command is not: the
            # same rule tests/test_dashboard.py's rail guard keys on.
            problems.append(f"{call} switches section")
    return problems


def test_every_action_button_is_a_command_not_a_section():
    (group,) = _actions(_Tree(_page())) or [None]
    assert group is not None, "no element with id=\"actions\""
    assert len(_buttons(group)) == len(ACTIONS)
    assert _command_problems(group) == []


def test_the_command_check_would_catch_a_section_button_among_the_commands():
    """CANARY for the test above. A section button moved into `#actions` and
    dressed as a command -- no `data-sec`, `nav-action`, an onclick -- passes
    every per-attribute check; only its handler says it switches section. The
    ACTIONS snapshot would notice too, until someone adds the call to it."""
    snippet = ('<div id="actions" role="group" aria-label="actions">'
               '<button class="nav-action" onclick="doExport()">X</button>'
               '<button class="nav-action" onclick="showLog()">Log</button>'
               '<button class="nav-action" data-sec="b" onclick="runB()">B</button>'
               '</div>')
    (group,) = _actions(_Tree(snippet))
    assert _command_problems(group) == ["showLog() switches section",
                                        "runB() claims a section"]


def test_the_sections_nav_keeps_every_section_it_had():
    """REGRESSION PIN (passes before the split): same ids, same handlers."""
    (nav,) = _navs(_Tree(_page()))
    assert _section_map(nav) == SECTIONS


def test_the_actions_group_carries_every_action_the_nav_used_to_hold():
    (group,) = _actions(_Tree(_page())) or [None]
    assert group is not None, "no element with id=\"actions\""
    calls = [b.attrs.get("onclick") for b in _buttons(group)]
    assert sorted(calls) == sorted(ACTIONS)


def test_no_rail_button_was_lost_or_duplicated_in_the_move():
    """REGRESSION PIN (passes before AND after): the union of both landmarks
    is exactly the snapshot, whichever side each button ended up on."""
    tree = _Tree(_page())
    sections, actions = {}, []
    for container in _navs(tree) + _actions(tree):
        for b in _buttons(container):
            if "data-sec" in b.attrs:
                assert b.attrs["data-sec"] not in sections, \
                    f"section {b.attrs['data-sec']} appears twice"
                sections[b.attrs["data-sec"]] = b.attrs.get("onclick")
            else:
                actions.append(b.attrs.get("onclick"))
    assert sections == SECTIONS
    assert sorted(actions) == sorted(ACTIONS)


def test_hiding_mindswarm_removes_only_its_section_button():
    """`MINDSWARM_NAV_BUTTON` is a separate constant spliced into the nav, so it
    is the one section button an edit to the markup alone would miss."""
    tree = _Tree(_page(swarm=False))
    (nav,) = _navs(tree)
    assert _section_map(nav) == {k: v for k, v in SECTIONS.items() if k != "ws-swarm"}
    assert not _unowned_nav_buttons(tree)
    shown = _Tree(_page(swarm=True))
    assert not _unowned_nav_buttons(shown), \
        "the MindSwarm button (MINDSWARM_NAV_BUTTON) is not a nav-link"
    (group,) = _actions(tree) or [None]
    assert group is not None, "no element with id=\"actions\""
    assert sorted(b.attrs.get("onclick") for b in _buttons(group)) == sorted(ACTIONS)


def test_the_command_headings_move_with_the_commands():
    """`Heavy` is information: it says which commands are expensive. It moves
    with them rather than being dropped, and neither heading stays behind in
    the navigation."""
    tree = _Tree(_page())
    (group,) = _actions(tree) or [None]
    assert group is not None, "no element with id=\"actions\""
    assert _headings(group) == ["Actions", "Heavy"]
    (nav,) = _navs(tree)
    assert not {"Actions", "Heavy"} & set(_headings(nav))


# --- the stylesheet ------------------------------------------------------------

def test_no_rule_keys_the_command_face_on_a_missing_data_sec():
    selectors = _every_selector(_stylesheet(_page()))
    assert "nav button:not([data-sec])" not in selectors


def test_the_selector_scan_would_catch_the_old_rule():
    """CANARY for the test above."""
    css = "a { color:red; }\n  nav button:not([data-sec]) { background:transparent; }"
    assert "nav button:not([data-sec])" in _every_selector(css)


def test_the_command_face_moved_to_the_actions_group():
    face = _merged(_top_level_rules(_stylesheet(_page())), "#actions button")
    assert (face.get("background"), face.get("color")) == ("transparent", "var(--muted)")


def test_commands_keep_the_rail_layout():
    face = _merged(_top_level_rules(_stylesheet(_page())), "#actions button")
    assert {k: face.get(k) for k in ("display", "width", "text-align")} \
        == {"display": "block", "width": "100%", "text-align": "left"}


def test_a_command_still_answers_the_pointer():
    """`#actions button` outranks the page-wide `button:hover` (an id beats a
    pseudo-class), so without a hover rule of its own the command's border
    would never move under the cursor."""
    hover = _merged(_top_level_rules(_stylesheet(_page())), "#actions button:hover")
    assert "border-color" in hover


def test_the_command_headings_look_like_the_section_headings():
    rules = _top_level_rules(_stylesheet(_page()))
    nav_head = _merged(rules, "nav .group")
    cmd_head = _merged(rules, "#actions .group")
    keys = ("font-size", "text-transform", "letter-spacing", "color")
    assert all(nav_head.get(k) for k in keys), nav_head
    assert {k: cmd_head.get(k) for k in keys} == {k: nav_head.get(k) for k in keys}


# --- the page script -----------------------------------------------------------

# The page's JS is written inside Python strings and quotes its selectors with
# single quotes throughout, which is the only spelling this reads.
_SELECTOR_CALL = re.compile(r"(\w+)\.(querySelector(?:All)?)\(\s*'([^']*)'\s*\)")


def _rail_selectors_without_data_sec(script) -> list:
    """Selectors that reach rail buttons without asking for `[data-sec]`: the
    only kind a split could silently narrow."""
    bad = []
    for obj, method, sel in _SELECTOR_CALL.findall(script):
        through_nav = obj == "navEl" or re.search(r"\bnav\b", sel)
        if through_nav and "button" in sel and "[data-sec]" not in sel:
            bad.append(f"{obj}.{method}('{sel}')")
    return bad


def test_the_page_script_never_selects_rail_buttons_without_data_sec():
    """REGRESSION PIN: the grep Bug 13 asked for, kept. Every script lookup of
    rail buttons asks for `[data-sec]`, so moving the commands out of `<nav>`
    narrows no query that meant to include them."""
    page = _page()
    script = page[page.index("<script>"):page.index("</script>")]
    assert _rail_selectors_without_data_sec(script) == []


def test_the_rail_selector_check_would_catch_an_unqualified_query():
    """CANARY for the test above."""
    script = ("const all = document.querySelectorAll('nav button');\n"
              "const first = navEl.querySelector('button');\n"
              "const on = navEl.querySelector('button[data-sec].on');")
    assert _rail_selectors_without_data_sec(script) == [
        "document.querySelectorAll('nav button')", "navEl.querySelector('button')"]


# --- spec 2.13, second half: the project-scope guard -----------------------------

_SCOPE_GUARD = "if (scope !== '' && PROJECT !== scope) throw StaleScope(scope);"


def _js_block(page, signature) -> str:
    start = page.index(signature)
    return page[start:page.index("\n  }", start)]


def _scope_order_problems(block) -> list:
    at_scope = block.find("const scope = PROJECT;")
    at_guard = block.find(_SCOPE_GUARD)
    at_return = block.find("return data;")
    if at_scope == -1 or at_guard == -1 or at_return == -1:
        return ["missing: " + ", ".join(
            name for name, at in (("scope capture", at_scope), ("scope check", at_guard),
                                  ("return", at_return)) if at == -1)]
    problems = []
    if not at_scope < block.find("await "):
        problems.append("the scope is captured after the request went out")
    if "await " in block[at_guard:]:
        problems.append("an await follows the scope check")
    if not at_guard < at_return:
        problems.append("the data is returned before the scope check")
    return problems


def test_apiget_checks_the_project_scope_after_its_last_await():
    """REGRESSION PIN: the guard spec 2.13 asks for is already here. The scope
    is captured before the request and compared after the LAST await (the body
    parse), so a picker switch at any point of the round trip is caught."""
    block = _js_block(_page(), "async function apiGet(path){")
    assert _scope_order_problems(block) == []


def test_the_scope_order_check_would_catch_a_guard_that_runs_too_early():
    """CANARY for the test above, run on mutations of the real function."""
    block = _js_block(_page(), "async function apiGet(path){")
    early = block.replace(_SCOPE_GUARD, "").replace(
        "const scope = PROJECT;", "const scope = PROJECT; " + _SCOPE_GUARD)
    assert "an await follows the scope check" in _scope_order_problems(early)
    assert _scope_order_problems(block.replace(_SCOPE_GUARD, "")) == \
        ["missing: scope check"]

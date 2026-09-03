# src/mindhive/dashboard_css.py
"""The dashboard page's own stylesheet.

Extracted verbatim from ``dashboard_page.py`` when that file reached 2396 lines,
following the pattern ``cat_board.CSS``, ``taskview.CARDS_CSS`` and
``theme.PALETTE_CSS`` already set: a page module owns its sheet as one named
constant, and the page splices it in at a placeholder.

It ships the whole sheet, including the reduced-motion guard — and that is a
constraint, not an accident. Every ``animation:`` this page declares itself is
declared here, and ``tests/test_dashboard.py`` scans each module in the package
for an animation switched on with no ``@media (prefers-reduced-motion: reduce)``
block in the SAME file. Splitting this sheet in two would put animations in one
file and their guard in another, and that test would say so.

What it deliberately does NOT declare: the colour palette (``theme.PALETTE_CSS``,
spliced at ``/*__PALETTE__*/`` — the first line of this constant), the cat
sprites (``cat_board.CSS``) and the session cards (``taskview.CARDS_CSS``). Those
are spliced in after this constant, by the page, so one legend has one home.
"""
CSS = """\
/*__PALETTE__*/
  /* The header's height, as a number the sheet can do arithmetic with. Four
     things have to start exactly below the header -- the sticky rail, the
     sticky section heading, the progress bar and `main`'s minimum height -- and
     the header is 52px empty and 68px once the faculty and session strips have
     arrived, so a constant is wrong for the first seconds of every session.
     Declared HERE and not in the palette: `theme.PALETTE` is the colour system,
     audited by tests that pair token values against hex literals, and a length
     in it would be a category error. Declared at all, rather than left to the
     `var()` fallbacks, because a custom property with no definition anywhere in
     the document is exactly what `test_each_document_defines_exactly_the_tokens_
     it_uses` exists to catch -- and it is right to: this one is only ever
     written by a script, and a script that fails to run would leave every rule
     using it silently taking its fallback.
     52px is the EMPTY header, which is what the first frame is served with;
     `measureHeader` overwrites it on the document element within that frame. */
  :root {
    --headh: 52px;
  }
  * { box-sizing: border-box; }
  /* An EXPLICIT stack, not a bare `system-ui`. On this machine `system-ui`
     already resolves to Segoe UI, but it resolves to whatever the OS nominates
     -- and the board is read for hours at 14px, so which face it lands on is
     not a detail to leave to the platform. Segoe UI Variable Text is the Win11
     face cut for body sizes and is what makes 500 and 700 read as two distinct
     weights rather than one smeared one; the rest of the chain names a real
     face per platform and ends at a generic. Every entry is a SYSTEM font: the
     page is served offline and must never reach for a font host. */
  body { font: 14px/1.5 "Segoe UI Variable Text", "Segoe UI", system-ui,
      -apple-system, "Helvetica Neue", Arial, sans-serif;
    margin: 0; background:var(--bg); color:var(--text); }
  /* A gradient, not a flat fill, and a STATIC one. The cheap way to get depth
     is to let the compositor rasterise a gradient once; the expensive way is to
     animate `background-position`, which repaints the whole band every frame.
     Both read the same at rest, so this page takes the free one. Built from
     tokens already in the palette, so it adds no hex literal. */
  header { padding: 12px 20px; background:linear-gradient(180deg,var(--raised),var(--surface));
    border-bottom:1px solid var(--line);
    display:flex; align-items:center; gap:14px; position:sticky; top:0; z-index:5; }
  /* The header has been sticky since it was written, and until now the content
     went UNDER it with nothing to say so: a 1 px border against a dark panel
     reads as a seam at rest and as nothing at all once rows are sliding beneath
     it. The shadow says which surface is in front.

     No transition, deliberately, and for the reason this sheet already gives
     twice: a box-shadow tween is a paint on a full-width band on every frame of
     it, which is exactly the trade the card hover refused. This one is written
     ONCE, at a threshold crossing, by a listener that touches the DOM only when
     the answer changes -- so it costs a single paint per crossing rather than
     twelve per fade. rgba, not a hex: a shadow is a black at an opacity, and
     there is no colour here to name in the palette. */
  header.deep { box-shadow: 0 10px 22px -16px rgba(0,0,0,.95); }
  /* The one piece of display type on the board, so it is allowed to be
     display type. 700 and NEGATIVE tracking: letters set loose at 17px read as
     a caption, and the smearing that rules out 600 in the rail is a 14px
     problem -- at 18px the heavy cut is exactly what is wanted. */
  header h1 { margin:0; font-size:18px; font-weight:700; letter-spacing:-.2px;
    /* The header is a flex row and everything in it is shrinkable by default,
       so a heavier, wider title is the first thing a crowded header folds: on
       the real board -- a long project name, two faculty cats and two live
       session chips -- "MIND HIVE" broke across two lines and pushed the whole
       bar to 60 px. The masthead does not wrap and does not shrink; the chips
       to its right already scroll. */
    white-space:nowrap; flex:0 0 auto; }
  header .dot { width:9px; height:9px; border-radius:50%; background:var(--dot-idle); }
  header .dot.busy { background:var(--warn); animation: pulse 1s infinite; }
  header .dot.ok { background:var(--ok); }
  /* Nowrap, for a reason that has nothing to do with typography. This text
     changes on every section load -- "ready" becomes "loading change log" --
     and as a shrinkable flex item it wrapped to two lines, took the header from
     52 px to 66 px, and pushed the entire board down 14 px and back on every
     click. That is a layout jump on the most-used interaction on the page, and
     the cheapest fix is to stop it being possible. */
  header #status { font-size:12px; color:var(--muted); white-space:nowrap; flex:0 0 auto; }
  /* The chip is a pause/resume control -- `cursor:pointer` has always said so
     -- and it was the one control on the header that did not move when the
     pointer reached it. The tint goes on the GROUND, not the text: this
     element's colour already carries three states (live, paused, stale) and a
     hover that repainted the text would be a fourth voice arguing with them. */
  header #live { font-size:12px; cursor:pointer; user-select:none; margin-left:auto;
    color:var(--ok); padding:2px 8px; border-radius:999px;
    transition: background-color .15s ease; }
  header #live:hover { background:var(--hover-tint); }
  header #live.paused { color:var(--dim); }
  /* Three consecutive failed live ticks (7.5 s): the chip stops claiming to be
     live. Colour + title only — toggleLive() rewrites this element's innerHTML,
     so any text state written here would vanish on the next pause/resume.
     `:not(.paused)` because both rules are one id + one class, so the later one
     wins a tie — and the later one was `.stale`. A chip that went stale and was
     then paused rendered amber "not refreshing" under the word "paused", and
     could not clear itself: markLiveHealth is unreachable while paused
     (refreshLive returns at `!liveOn`). Paused is a state the user CHOSE; it
     outranks a health signal about a poll that is no longer running. */
  header #live.stale:not(.paused) { color:var(--warn); }
  header #live .blip { animation: pulse 1.6s infinite; }
  header #synced { font-size:11px; color:var(--dim); min-width:118px; text-align:right; }
  header #projwrap { font-size:12px; color:var(--muted); display:flex; align-items:center; gap:6px; }
  header #projwrap select { padding:3px 6px; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.3} }
  /* `50px` was the header's height written down as a constant, and the header
     is not one: it is 52 px empty and 68 px once the faculty and session strips
     arrive. Every offset that has to clear it now reads the MEASURED height,
     written to `--headh` by the page (see `measureHeader`) and re-written by a
     ResizeObserver, so nothing on the board overlaps a header that grew. The
     fallback in each `var()` is the empty height, which is what the first frame
     is served with anyway. */
  main { display:grid; grid-template-columns: 220px 1fr;
    min-height: calc(100vh - var(--headh)); }
  /* The rail STAYS, and this is the largest single thing the board was missing.
     `nav` was an ordinary grid item in a document that scrolls itself, so it
     scrolled away with the content -- and the content is not a screenful:
     measured on the real store, the Log section is 42016 px tall in a 900 px
     viewport. That is 46 screens with navigation on the first one only, which
     is why Round E had to add a "back to top" button at all: getting anywhere
     else meant returning to the top first.

     `align-self:start` is load-bearing rather than tidiness. A grid item
     stretches by default, so nav was already as tall as the panel -- and an
     element as tall as its scroll container has nowhere to stick to, so
     `position:sticky` alone would have changed nothing at all. `overflow-y`
     covers the other case: a rail taller than the viewport (26 buttons at a
     small window height) gets its own scrollport instead of clipping.

     `sticky` is a POSITIONED value, so it goes on being the offsetParent that
     `#navdot` resolves against and `markSection`'s offsetTop arithmetic is
     untouched -- which is why the `position:relative` this replaces is gone
     rather than kept beside it. */
  nav { background:var(--rail); border-right:1px solid var(--line); padding:12px;
    position:sticky; top:var(--headh); align-self:start;
    max-height:calc(100vh - var(--headh)); overflow-y:auto; }
  nav .group { font-size:11px; text-transform:uppercase; letter-spacing:.6px; color:var(--dim);
    margin:14px 6px 4px; }
  /* A `button` does NOT inherit the document font. Every other control here was
     told to (`input, select, textarea { font:inherit }` below); buttons never
     were, so they took the browser's own UI font. Measured in the served page,
     not inferred: all 26 buttons computed to Arial 13.3333px against a
     system-ui 14px document -- the nav, the most-used surface on the board,
     was the one part of it rendered in a different typeface. */
  /* Every button on the board. This used to be `font:inherit` alone, and a
     `<button>` with nothing else of its own renders as an OS control -- a pale
     grey box sitting in a dark panel. Six ship on this page (Enrich, Search,
     Append, show all, and the two direction toggles) and each looked pasted in
     from another application; none of them answered the cursor either, since a
     button's default cursor is an arrow. They now wear the rail's face. `nav
     button` and `button.mini` keep theirs: both out-specify this, and `.callbtn`
     sets its own ground, so the three that were already dressed are untouched. */
  button { font: inherit; cursor:pointer; padding:6px 12px;
    background:var(--raised); color:var(--text);
    border:1px solid var(--line); border-radius:6px;
    transition: background-color .15s ease, border-color .15s ease; }
  button:hover { background:var(--hover); border-color:var(--accent-edge); }
  button:active { translate:0 1px; }
  nav button { display:block; width:100%; text-align:left; margin:4px 0; padding:8px 10px;
    background:var(--raised); color:var(--text); border:1px solid var(--line); border-radius:6px; cursor:pointer;
    /* Weight and tracking, not size: the rail is fixed at 220px and a larger
       label would wrap "Skill & Plugin" onto two lines. 500 reads as firm at
       14px without the smearing 600 gives at this size on Windows. */
    font-weight:500; letter-spacing:.15px;
    /* Compositor-only movement plus two colour fades. `translate` is its own
       property here rather than a `transform` shorthand so nothing later has to
       rewrite the whole transform to add a second track. */
    transition: background-color .15s ease, border-color .15s ease, translate .15s ease; }
  nav button:hover { border-color:var(--accent-edge); translate:2px 0; }
  nav button:active { translate:1px 0; }
  /* Keyboard focus must be visible and must NOT show on a mouse click --
     `:focus-visible`, not `:focus`. No transition: a focus ring that fades in
     arrives after the eye has already moved. */
  /* The last two entries are not native controls, and that is the whole reason
     they are here. A `<div class="card go" role="button" tabindex="0">` promises
     a button and gets none of one for free: measured in the browser, a focused
     door card computed `outline-style:none` from this sheet and fell back to
     whatever ring the BROWSER draws -- so the two cards that lead somewhere were
     the only controls on the board wearing a focus ring the design never chose.
     The skip link is the first thing a keyboard reaches on the page and would
     have had the same problem. */
  button:focus-visible, input:focus-visible, select:focus-visible,
  textarea:focus-visible, .card.go:focus-visible,
  a:focus-visible { outline:2px solid var(--link); outline-offset:2px; }
  /* No `:disabled` rule, and no `:not(:disabled)` qualifier on the hover: nav
     buttons are never disabled any more. setBusy used to grey the entire nav out
     for the duration of a fetch — which is what made switching section mid-load
     impossible, and what made the nav feel dead for the 1-2 s a section costs. */
  nav button:hover { background:var(--hover); }
  /* Sixteen of these buttons take you somewhere; ten of them DO something, and
     all twenty-six wore one face. The actions step back -- no ground, quieter
     label -- so the rail reads as navigation with commands under it rather than
     twenty-six equal choices. `:not([data-sec])` is the same attribute the
     active marker keys on, so there is one definition of "this button owns a
     section" and not two. Stepping back is not being disabled: the hover, the
     focus ring and the travel are all still theirs. */
  nav button:not([data-sec]) { background:transparent; color:var(--muted); }
  /* WHICH section is on screen -- the one thing a 26-button rail could not say.
     Recessed rather than brightened: `--sunken` reads as pressed into the rail,
     and it leaves `--hover` free to go on meaning "under the pointer" instead
     of colliding with it. `aria-current` is set BESIDE this class, never
     instead of it: a colour is invisible to a screen reader, and a state only
     sighted users can perceive is half a state. */
  /* A gradient that FADES OUT to the right, so the recess reads as light
     thrown from the indicator bar rather than as a second, competing block of
     colour beside it. Static, therefore rasterised once and free -- the same
     bargain the header takes, and the reason neither of them animates a
     `background-position`. */
  nav button.on { background:linear-gradient(90deg,var(--sunken),transparent);
    color:var(--link); }
  /* The travelling half. `translate` is a compositor property, so moving the
     bar from one button to another costs no layout and no paint -- which is the
     whole reason this is one bar that moves rather than sixteen that fade.
     Height is written straight in with no transition: it changes only if a
     label wraps, and animating it would be a layout tween bought for nothing. */
  #navdot { position:absolute; left:5px; top:0; width:3px; height:0;
    border-radius:2px; background:var(--link); opacity:0; translate:0 0;
    transition: translate .28s cubic-bezier(.16,1,.3,1), opacity .2s ease; }
  #navdot.lit { opacity:1; }
  /* `min-width:0` on a grid item is the one line that stops one long word from
     setting the width of the whole board. A grid item's automatic minimum size
     is its min-content, so the `1fr` track grows to whatever the widest
     unbreakable token in the panel is, and the document grows with it -- which
     is how a single comma-joined path put a horizontal scrollbar under every
     section of the board. It is paired with `overflow-wrap:break-word` further
     down, and neither half is sufficient alone: this one bounds the TRACK, that
     one gives the token somewhere to break so it does not simply overflow the
     bounded track instead. */
  section.panel { padding:18px 22px; min-width:0; }
  /* The keyboard's way in. Measured on the served page: 27 tabbables stand
     between the address bar and the first control inside the panel, because the
     rail is 26 buttons and it comes first in the source. That is the standard
     price of a sidebar and the standard answer is this link -- off-screen until
     it is focused, first in the DOM, and pointed at the panel.
     `clip` rather than `display:none`, for the same reason `.sr-only` uses it:
     a control that is not rendered is not focusable, and a skip link that
     cannot be focused is decoration. */
  #skip { position:fixed; left:12px; top:-60px; z-index:7;
    background:var(--raised); color:var(--link); text-decoration:none;
    border:1px solid var(--accent-edge); border-radius:6px; padding:8px 12px;
    transition: top .15s ease; }
  #skip:focus { top:12px; }
  /* Where you are in a section, for the sections where that is a real question.
     A 42016 px Log in a 900 px viewport gives the scrollbar thumb about 19 px
     of travel, and a thumb that small stops being a position indicator. This is
     one 2 px band under the header, and it is `scale` on a compositor property
     rather than `width`, which would relayout the band on every scroll event.
     Only the OPACITY transitions: the bar itself must track the wheel exactly,
     and a transition on the scale would make it lag behind the content it is
     reporting on -- an indicator that is wrong while it is catching up.
     `.on` is written only for a document that actually scrolls past a screen,
     so short sections show nothing rather than a bar pinned at 100%. */
  #prog { position:fixed; left:0; top:var(--headh); width:100%; height:2px;
    z-index:6; background:var(--link); transform-origin:0 50%; scale:0 1;
    opacity:0; pointer-events:none; transition: opacity .2s ease; }
  #prog.on { opacity:.9; }
  /* Back to the top of a long section. The board scrolls the DOCUMENT, not the
     panel, so at 239 change-log entries the rail scrolls away with the content
     and there is nothing left to click -- the way back is a flick of the wheel
     or the Home key, and the Home key does not work while a list has focus.
     Hidden until it is worth having. `opacity` and `translate` only, so
     appearing costs a composite rather than a layout, and `pointer-events`
     rather than `display` so the transition has something to run on. */
  #totop { position:fixed; right:22px; bottom:22px; z-index:6;
    opacity:0; translate:0 8px; pointer-events:none;
    transition: opacity .2s ease, translate .2s ease; }
  #totop.up { opacity:1; translate:0 0; pointer-events:auto; }
  /* A section swap is the one moment the entire panel becomes something else,
     and it used to happen with no transition at all: the old section's contents
     were simply gone and the new one's were simply there. This is the smallest
     honest way to say "new place" -- opacity and `translate`, both compositor
     properties, on ONE element, for 220 ms.
     It is keyed to `markSection`, which runs once per ENTRY, and never to
     `only()`, which runs on every paint: the live poll repaints Overview every
     2.5 s, and an entrance wired there would make the board breathe at the user
     forever. `translate` is its own property rather than a `transform`
     shorthand so a later track can be added without rewriting this one. */
  @keyframes panelIn { from { opacity:0; translate:0 6px; }
                       to   { opacity:1; translate:0 0; } }
  section.panel.swap { animation: panelIn .22s cubic-bezier(.16,1,.3,1); }
  pre#out { white-space:pre-wrap; background:var(--sunken); border:1px solid var(--line); border-radius:8px;
    padding:14px; min-height:120px; max-height:62vh; overflow:auto; }
  iframe#graph { width:100%; height:72vh; border:1px solid var(--line); border-radius:8px; background:var(--paper); }
  /* Dressed early, and never given a reaction. `select` was the worse half:
     it opens a menu on click and was still showing the OS arrow, so the one
     control in the header that changes what the whole board is looking at
     denied being a control at all.
     The BORDER lights, not the ground. These elements hold a value the user
     typed, and a ground tint on a filled field argues with the focus ring that
     lands on the very same element a moment later; an edge does not. */
  input, select, textarea { background:var(--sunken); color:var(--text); border:1px solid var(--line);
    border-radius:6px; padding:6px 8px; font:inherit;
    transition: border-color .15s ease; }
  input:hover, select:hover, textarea:hover { border-color:var(--accent-edge); }
  select { cursor:pointer; }
  .row { margin:8px 0; display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
  .hidden { display:none; }
  .err { color:var(--bad); }
  /* Loading skeleton. Placeholders rather than a spinner: the shape says content
     is coming and roughly what shape it has; a spinner only says something is
     happening, which the busy dot already says. Never painted before SKEL_MS —
     under that the eye reads a placeholder as a flicker, not as progress.

     FOUR pieces, one per thing a section actually paints: a text line (`bar`), a
     stat card (`tile`), a list row (`line`), and one monospace block (`pane`).
     The sizes were MEASURED in a browser against the real panels rather than
     inferred from the markup, and the measurements say two different things.

     `tile` matches: six of them reproduce Overview's grid exactly (200px wide at
     this width, against a real card of 89-95px depending on whether the label
     wraps — 95 here, the taller end, so the panel never GROWS when content
     lands). `line` does not, and cannot: a real `ul.hits > li` is 115-447px in
     the Log, Improvements and Sops panels, while a Sessions `.tcard` is 42. No
     fixed height avoids a reflow across that range, so this one is not trying
     to — its job is to say "a list is coming", and it is deliberately shorter
     than the content rather than a screenful of shimmer. */
  .skel .bar, .skel .tile, .skel .line, .skel .pane {
    background:linear-gradient(90deg,var(--shimmer-base) 25%,var(--shimmer-sheen) 37%,var(--shimmer-base) 63%);
    background-size:400% 100%; animation: shimmer 1.4s ease infinite; }
  .skel .bar { height:14px; margin:10px 0; border-radius:6px; }
  .skel .bar.w60 { width:60%; }
  .skel .bar.w40 { width:40%; }
  .skel .tile { height:95px; border-radius:10px; }
  .skel .line { height:64px; margin:8px 0; border-radius:8px; }
  .skel .pane { min-height:120px; height:34vh; border-radius:8px; }
  @keyframes shimmer { 0%{background-position:100% 0} 100%{background-position:0 0} }
  /* The screen-reader channel. Off-screen, never display:none or hidden — a
     live region that is not rendered at all is not announced at all. */
  .sr-only { position:absolute; width:1px; height:1px; padding:0; margin:-1px;
    overflow:hidden; clip:rect(0 0 0 0); white-space:nowrap; border:0; }
  .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; }
  /* The entrance, and the two reasons it is affordable.
     ONE: every track is a compositor property -- `opacity`, `translate`,
     `scale` -- so three of them choreographed together cost what one costs.
     They are individual properties rather than a `transform` shorthand
     precisely so they COMPOSE: two rules both writing `transform` fight over
     one slot, while these stack.
     TWO: it runs on SEVEN elements, once, on arrival. The type chips below the
     grid are ~30 and deliberately do not join in -- measured on this page's own
     sprites, 40 simultaneously animating elements cost +183 ms of main-thread
     work per 3 s while one costs nothing readable. The stagger is what makes
     seven feel like a sequence; thirty would make it feel like a load screen.
     `backwards` so a card is already invisible during its delay rather than
     appearing, waiting, then animating. */
  @keyframes cardIn {
    from { opacity:0; translate:0 10px; scale:.98; }
    to   { opacity:1; translate:0 0;    scale:1; }
  }
  .cards.enter .card { animation: cardIn .38s cubic-bezier(.16,1,.3,1) backwards; }
  /* The value that just moved. One or two elements per tick, 500 ms, and it
     answers a question the panel otherwise leaves to the reader's memory:
     WHICH number changed. `scale` is compositor work; the colour is a paint on
     one text node, which is the whole reason this is a handful of nodes and not
     the grid. `--ok-strong` is the saturated green the palette already keeps
     for a mark that has to read small. */
  @keyframes valueBump {
    0%   { scale:1;    color:var(--ok-strong); }
    35%  { scale:1.07; color:var(--ok-strong); }
    100% { scale:1; }
  }
  .cards .big.bump, .types .pill b.bump { animation: valueBump .5s ease-out;
    display:inline-block; }
  .cards.enter .card:nth-child(2) { animation-delay:35ms; }
  .cards.enter .card:nth-child(3) { animation-delay:70ms; }
  .cards.enter .card:nth-child(4) { animation-delay:105ms; }
  .cards.enter .card:nth-child(5) { animation-delay:140ms; }
  .cards.enter .card:nth-child(6) { animation-delay:175ms; }
  .cards.enter .card:nth-child(7) { animation-delay:210ms; }
  .card { background:linear-gradient(180deg,var(--inset),var(--sunken));
    border:1px solid var(--line); border-radius:10px; padding:14px 16px;
    transition: border-color .18s ease, translate .18s ease; }
  /* Lift on `translate` and change one border colour. The obvious alternative --
     animating `box-shadow` -- is a paint on every frame of the transition for
     every card the pointer crosses; a 2px compositor nudge reads as the same
     gesture and costs nothing. */
  .card:hover { border-color:var(--ok-edge); translate:0 -2px; }
  /* Two of the seven numbers are also doors: sessions and memory each have a
     section that explains them. Those two say so, in a LINK-coloured edge that
     does not read the same as the plain hover lift above -- "this goes
     somewhere" and "this is under the pointer" are different claims. The other
     five stay inert on purpose: `cursor:pointer` is a promise, and there is
     nowhere for a hash-chain verdict or a timestamp to lead. */
  .card.go { cursor:pointer; }
  .card.go:hover { border-color:var(--link); }
  /* Tabular figures. These numbers change under a 2.5 s live poll, and
     proportional digits are different widths -- so `43586` becoming `43591`
     re-measures the text and nudges everything beside it. Lining figures keep
     a changing count still. */
  /* 700 with the figures pulled together. These are the numbers the board
     exists to show, and at 26px a heavy cut costs nothing in legibility while a
     light one reads as provisional. */
  .card .big { font-size:26px; font-weight:700; letter-spacing:-.4px;
    font-variant-numeric:tabular-nums; }
  .card .lbl { font-size:12px; color:var(--muted); margin-top:2px; }
  .card .big.ok { color:var(--ok); } .card .big.bad { color:var(--bad); }
  /* The SOP-injection strip. Rendered only for a DEGRADED verdict (the server
     sends null for `verbatim`), so it never competes with the cards for
     attention in the healthy case — and it is not a `.card`, because the cards
     are this namespace's counts and this verdict is machine-global. */
  .injwarn { border:1px solid var(--warn); border-radius:10px; padding:12px 14px;
    margin:0 0 12px; background:var(--sunken); font-size:13px; }
  .injwarn b { color:var(--warn); }
  .injwarn .lbl { display:block; font-size:12px; color:var(--muted); margin-top:6px; }
  .types { margin-top:14px; display:flex; flex-wrap:wrap; gap:8px; }
  .pill { background:var(--raised); border:1px solid var(--line); border-radius:999px; padding:3px 10px; font-size:12px; }
  .pill b { color:var(--text-strong); }
  .pill.est { color:var(--warn); border-color:#5a4a1f; }
  .pill.triclaude { color:var(--link); border-color:#2c4a75; font-weight:700; }
  .pill.tridism { color:var(--faint); }
  .pill.sr { color:#c9a6ff; border-color:#5b3f8a; background:#191227; }
  /* A READING COLUMN, not the width of the window. Measured on the real store
     at 1440px: a change-log row ran 1176 px wide and its prose came to 152
     characters per line, which is a shade under twice the widest line anybody
     sets prose at on purpose. The eye loses the start of the next line at that
     measure, and this is the section with 270 rows in it.
     `ch` is the right unit precisely because the cap is about characters: it
     follows the font, so the column keeps its measure whatever the reader's
     zoom does to the pixels. `min()` keeps it a cap and never a floor, so a
     narrow window still gets the full width it has.
     68 AND NOT 82, which is what this first shipped as, because `ch` resolves
     against the element it is written on and that is not the element the text
     is in: the row is the document's 14px and the headline inside it is 13px,
     so a row capped at 82ch measured 96 characters of headline -- past the
     comfortable band again, by a different route. 68ch of row is ~73 characters
     of headline and ~68 of snippet, both inside it. Measured in the browser
     rather than converted on paper.
     The cap is on the ROW, not on the paragraph inside it: capping the text
     alone leaves each row a full-width box with its content huddled in the left
     two thirds, which reads as a layout that broke rather than a column that
     was chosen. */
  .hits { list-style:none; padding:0; margin:0; }
  .hits li { max-width:min(100%, 68ch);
    background:var(--sunken); border:1px solid var(--line); border-radius:8px; padding:10px 12px; margin:8px 0;
    /* The row under the pointer, in a list that is 46 screens long. The GROUND
       lifts and the cursor deliberately does NOT change: a hand would promise a
       click these rows do not answer, while a ground that follows the pointer
       only says which row the eye is on -- which is the thing that is genuinely
       hard once the rail and the heading are both above the fold. */
    transition: background-color .15s ease; }
  .hits li:hover { background:var(--inset); }
  /* A row whose METADATA goes beside the prose instead of under it. One class,
     asked for by the renderer that has the shape for it: the change log is 270
     rows of a sentence followed by a file list and a timestamp, and stacking
     those three made every row four lines taller than the sentence needed --
     measured, 62526 px of document for 270 entries. Beside it, the meta costs
     nothing at all while the prose is longer than two lines, which it almost
     always is.
     The prose column is the same 68ch cap the single-column rows take, so the
     measure does not change between sections; the meta column is bounded in
     `em` because what it holds is a wrapped list of paths, not prose, and it
     should take the width it needs rather than a share of the row. Both tracks
     are `minmax(0, …)`: a grid track's automatic minimum is min-content, which
     is the same trap `section.panel` needs `min-width:0` for.
     Placement is by column only. The three children then land where the
     document already puts them -- headline in column one, file list and stamp
     in column two, in source order -- so a section that renders a fourth child
     gets a sensible answer instead of an overridden one. */
  .hits li.two { max-width:none; display:grid; align-items:start;
    grid-template-columns:minmax(0, 68ch) minmax(0, 26em); column-gap:20px; }
  .hits li.two .file { grid-column:1; }
  .hits li.two .snip { grid-column:2; }
  /* `.file` is named for the one row in sixteen that really holds a path. The
     other fifteen call sites put the row's HEADLINE in it -- the change-log
     entry's own sentence, an SOP title, a skill call, a session head -- so the
     class that dresses a path is what dresses most of the prose on this board.
     `word-break:break-all` is right for a path and ruinous for a sentence: it
     offers the line-breaker a break between every pair of characters, and a
     greedy line-breaker takes them, so words are chopped at the right margin
     whether or not they would have fitted on the next line. Measured on the
     real Log: `the com/ment`, `expande/d skill`, `REPRESENTATI/VES`, several
     per entry across 270 of them.
     `overflow-wrap:break-word` is the difference between "may break anywhere"
     and "may break anywhere ONLY if there is nowhere else to break": a sentence
     wraps at its spaces, and a 200-character path with no spaces in it still
     breaks rather than overflowing. Every other document in this package
     already had it (graphview, swarm_page, research_page, workspace_page); the
     two rules here were the outliers. */
  /* Dressed as what it IS, which is the row's headline. It was 12px in
     `--link`, the blue this board otherwise spends on a path and on a name that
     can be clicked -- and fifteen of the sixteen rows wearing it are neither.
     Blue text that is not a link is the same kind of promise `cursor:pointer`
     over a dead surface is, and the census that exists for the second one has
     no opinion about the first.
     So: body colour, and 13px rather than 12 -- this is the most-read text on
     the board and it was set two steps below the body size it sits in. The
     colour on these rows now comes from the badges, which is where it belongs:
     a change-log category, an SOP action, a status dot.
     `.path` is the exception, and it is one call site (Recall's file column),
     which is why the blue moved to a class of its own instead of staying the
     default that fourteen other rows had to live with. */
  .hits .file { font-size:13px; color:var(--text); overflow-wrap:break-word; }
  .hits .path { font-size:12px; color:var(--link); }
  /* `pre-wrap` keeps the line breaks a snippet was written with, and breaks at
     spaces -- but a token with NO space in it cannot break at all, and one of
     those stretches the whole board sideways: the panel is a `1fr` grid item,
     so its track grows to its min-content, and the page grows with it. The
     ledger really does hold such tokens. `hive log --files` is space-separated
     (nargs='*'), so an entry that passed a comma-joined string recorded ONE
     bogus path -- `a.py,b.py,c.py,...` -- measured here at 1209 px of
     unbreakable text, which put a horizontal scrollbar under every section.
     CORRECTS what this comment said when it shipped: it claimed `anywhere`
     "breaks only what cannot otherwise fit, so ordinary snippets go on wrapping
     at their spaces". That is what `break-word` does; `anywhere` also chops
     ordinary words, and the Log was full of the evidence. The swap is safe here
     for a reason the swap could not rely on before: `break-word` leaves the
     element's MIN-CONTENT at the width of the longest token, which is the half
     that grew the track -- so the track is bounded by `min-width:0` on the
     panel instead, and the token itself, having nowhere else to break, still
     breaks. Both halves are measured by `probe_no_sideways_scroll`. */
  .hits .snip { white-space:pre-wrap; margin-top:4px; overflow-wrap:break-word; }
  .hits .dup { color:var(--faint); }
  .snip.one { white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .skchip { display:inline-block; cursor:pointer; color:var(--link-bright); background:#12182a;
    border:1px solid #2a3550; border-radius:6px; padding:1px 7px; margin:2px 3px 0 0; }
  .skchip:hover { background:var(--hover-tint); border-color:var(--accent-edge); color:#dfeaff; }
  .statusDot { font-size:11px; margin-right:5px; vertical-align:middle; }
  .statusDot.sok { color:var(--ok-strong); }
  .statusDot.sbad { color:var(--bad-strong); }
  .statusDot.snone { color:var(--dot-idle); }
  /* called, but no outcome was ever witnessed (a slash invocation fires no
     tool, so nothing reads a result). `--warn` is the same colour `.pill.est`
     uses for "there is a number here, but it is not measured". */
  .statusDot.sunobs { color:var(--warn); }
  .legitem { cursor:pointer; padding:1px 5px; border-radius:6px; }
  .legitem:hover { background:var(--hover-tint); }
  .legitem.on { background:#233; outline:1px solid var(--accent-edge); }
  .callcount { font-size:11px; color:var(--muted); margin-left:3px; }
  .callbtn { margin-left:8px; cursor:pointer; background:#12281a; border:1px solid var(--ok-edge);
    color:#9f9; border-radius:6px; font-size:11px; padding:0 7px; }
  .callbtn:hover { background:#1b3a26; color:#dfffe6; }
  /* The press, for every surface that already answered the pointer and then
     went still under the click. `button` and `nav button` have had this since
     Round C; these six are the rest of the board catching up, and the gap was
     visible in exactly the place it matters -- a skill chip and a legend row
     are clicked repeatedly, and neither gave any sign that the click had
     registered until the panel finished re-rendering a second later.
     One pixel down, and on five of the six it is INSTANT: none of them declares
     a transition that names `translate`, so the offset lands on the frame the
     pointer goes down and leaves on the frame it comes up, which is what a
     press is. `.card.go` is the exception and keeps its own 180 ms travel,
     because its rule already transitions `translate` for the hover lift and a
     second, contradictory timing on one property is not something CSS can
     express -- nor is it worth splitting the rule for. */
  .skchip:active, .legitem:active, .callbtn:active, .rmstep:active,
  header #live:active, .card.go:active { translate:0 1px; }
  .skmeta { margin:10px 0; }
  .skmeta b { color:var(--link); }
  .skbody { white-space:pre-wrap; background:var(--sunken); border:1px solid var(--line);
    border-radius:8px; padding:10px 12px; margin-top:4px; max-height:52vh; overflow:auto; }
  /* The heading STAYS, for the same reason the rail does. It is the only thing
     on screen that names the section, and in a 42016 px list it is gone after
     the first flick of the wheel -- so from row 6 to row 270 the panel is an
     unlabelled wall of rows.
     Full-bleed by negative margin, because a sticky heading with the panel's
     own padding beside it lets rows slide up through a 22 px gutter on each
     side. The background is `--bg`, the page's own ground: the panel declares
     no background of its own, so a solid band of it is seamless at rest and
     opaque while rows pass under it. */
  h2.h { position:sticky; top:var(--headh); z-index:4;
    background:var(--bg); margin:0 -22px 12px; padding:8px 22px 8px;
    font-size:16px; }
  .glyph { font-weight:700; margin-right:6px; }
  .glyph.res { color:var(--ok); } .glyph.unres { color:var(--bad); }
  .glyph.watch { color:var(--warn); } .glyph.dis { color:var(--faint); }
  .legend { font-size:12px; color:var(--muted); margin:2px 0 10px; }
  .ovdone { color:var(--ok); font-weight:600; }
  .ovpend { color:var(--faint); }
  /* An open question is a warning, not a pending step: same row shape, a
     colour that says the work may be blocked rather than merely queued. */
  .ovblock { color:var(--warn); font-weight:600; }
  .ovtask { color:var(--faint); text-transform:uppercase; font-size:10px;
            letter-spacing:.4px; margin-right:4px; }
  .ovspin { color:var(--warn); display:inline-block; animation: pulse 1.6s infinite; }
  /* Interactive roadmap panel (Oversee): ask + progress bar + stepper */
  .rmwrap { margin:8px 0 4px; padding:8px 10px; background:var(--inset);
    border:1px solid var(--line); border-radius:8px; }
  .rmask { margin-bottom:6px; }
  .rmint { color:var(--warn); font-size:12px; margin:2px 0 6px; }
  .rmbar { height:6px; background:var(--raised); border-radius:999px; overflow:hidden; }
  .rmfill { height:100%; background:var(--ok); border-radius:999px;
    transition:width .4s; }
  .rmstep { position:relative; padding:3px 0 3px 22px; cursor:pointer;
    border-radius:6px; transition: background-color .15s ease; }
  /* A step opens its detail on click. The row is the target, so the row is what
     lights -- a tint on the ground rather than on the glyph, which is already
     spending its colour on done / active / pending / skipped. */
  .rmstep:hover { background:var(--hover-tint); }
  .rmstep::before { content:''; position:absolute; left:7px; top:0; bottom:0;
    width:2px; background:var(--line); }
  .rmstep.rmfirst::before { top:50%; }
  .rmstep.rmlast::before { bottom:50%; }
  .rmnode { position:absolute; left:0; width:16px; text-align:center;
    background:var(--inset); }
  .rmnode.rmdone { color:var(--ok); }
  .rmnode.rmactive { color:var(--warn); animation: pulse 1.6s infinite; }
  .rmnode.rmpend { color:var(--faint); }
  .rmnode.rmskip { color:var(--dimmest); }
  .rmdetail { color:var(--muted); font-size:11px; margin:2px 0; }
  /* Prompt enrichment panel (Oversee): toggle + questions + composed prompt */
  .enwrap { margin:0 0 12px; padding:10px 12px; background:var(--sunken);
    border:1px solid var(--line); border-radius:8px; }
  .enwrap textarea { width:100%; box-sizing:border-box; background:var(--inset);
    color:var(--field-text); border:1px solid var(--line); border-radius:6px;
    padding:6px 8px; font:inherit; margin-top:6px; }
  .ensec { margin:8px 0 2px; }
  .enq { margin:6px 0; }
  .enq input { width:70%; background:var(--inset); color:var(--field-text);
    border:1px solid var(--line); border-radius:6px; padding:2px 8px;
    font:inherit; margin-top:2px; }
  .enok { color:var(--ok); font-weight:700; }
  .enbad { color:var(--bad); font-weight:700; }
  .enout { background:var(--inset); border:1px solid var(--line); border-radius:8px;
    padding:8px 10px; white-space:pre-wrap; max-height:40vh; overflow:auto; }
  button.mini { font-size:11px; padding:1px 8px; margin-left:8px; background:var(--raised);
    color:var(--muted); border:1px solid var(--line); border-radius:999px; cursor:pointer;
    transition: color .15s ease, border-color .15s ease; }
  button.mini:hover { color:var(--bad); border-color:var(--bad); }
  /* MN-13: the faculty cat board, inline in the header. Every cat and panel rule
     comes from `cat_board.CSS`; this is only the container's own box, which differs
     from the swarm page's full-width strip. `margin-left:auto` pushes it to the
     header's trailing edge so it never shoves the project picker around as cats
     appear and vanish. */
  #mn-cats { display:flex; align-items:flex-end; gap:5px; margin-left:auto;
    flex:0 1 auto; overflow-x:auto; max-width:52%; }
  /* The session strip sits beside the faculty one rather than under it: this
     header is a single row, and a second band would push the whole page down
     every time a Claude starts working. Narrower cap so the faculty cats — the
     fixed seven — never get squeezed out by an unbounded session list. */
  #mn-sessions { display:flex; align-items:center; gap:5px; flex:0 1 auto;
    overflow-x:auto; max-width:34%; margin-left:8px; }
  /* This page's OWN motion, guarded separately from the one that arrives with
     `cat_board.CSS`: that sheet deliberately names only the shared sprite, so a
     reduced-motion user who got only the cat guard would keep every pulse this
     page declares itself. Each of these animations is switched on by a rule at
     least as specific, and a media query adds no specificity — hence !important. */
  @media (prefers-reduced-motion: reduce) {
    header .dot.busy, header #live .blip, .ovspin, .rmnode.rmactive,
    .skel .bar, .skel .tile, .skel .line, .skel .pane,
    .cards.enter .card, .cards .big.bump,
    .types .pill b.bump, section.panel.swap { animation:none !important; }
    /* The hover feedback goes too. The colour and the lift still APPLY -- the
       hover rules are untouched -- they just arrive instantly instead of
       travelling, which is what a reduced-motion request asks for: state
       without movement, not state without feedback. */
    .rmfill, nav button, .card, button.mini,
    #navdot, button, header #live, .rmstep, #totop,
    /* Round F. The row still lights under the pointer, the skip link still
       arrives, the progress bar is still there -- they simply stop travelling
       to it. `#prog`'s own SCALE was never transitioned in the first place, for
       a reason that has nothing to do with this setting: it has to track the
       wheel exactly. */
    .hits li, #skip, #prog,
    input, select, textarea { transition:none !important; }
  }
  /* One breakpoint, and it exists because this window is resizable. The board
     is a 220 px rail beside a `1fr` panel, and below about 860 px the panel has
     less room than the rail it sits next to -- the change-log rows measured 152
     characters at 1440 px come down to 40 at 780 px, which is the opposite
     failure and just as unreadable.
     The rail becomes a band across the top rather than a drawer behind a
     button: a drawer is a second interaction to reach navigation that is one
     click away today, and this board's whole Round F is about not making the
     reader go and find the rail. It stays sticky, so it is still there at row
     270; `#navdot` goes, because it is a bar that travels DOWN a column and
     there is no column any more -- the `.on` gradient is what marks the active
     section here, and it is legible in either direction. */
  @media (max-width: 860px) {
    main { grid-template-columns: 1fr; }
    nav { border-right:0; border-bottom:1px solid var(--line);
      max-height:40vh; }
    #navdot { display:none; }
    /* The measure cap and the two-column row are both answers to having MORE
       width than the text wants. Below the breakpoint there is less, so both
       get out of the way rather than fighting for a share of it. */
    .hits li { max-width:100%; }
    .hits li.two { display:block; }
  }
/*__CAT_CSS__*/
  /*__TASKS_CSS__*/"""

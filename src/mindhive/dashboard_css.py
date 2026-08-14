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
  * { box-sizing: border-box; }
  body { font: 14px/1.5 system-ui, sans-serif; margin: 0; background:var(--bg); color:var(--text); }
  header { padding: 12px 20px; background:var(--surface); border-bottom:1px solid var(--line);
    display:flex; align-items:center; gap:14px; position:sticky; top:0; z-index:5; }
  header h1 { margin:0; font-size:17px; letter-spacing:.5px; }
  header .dot { width:9px; height:9px; border-radius:50%; background:var(--dot-idle); }
  header .dot.busy { background:var(--warn); animation: pulse 1s infinite; }
  header .dot.ok { background:var(--ok); }
  header #status { font-size:12px; color:var(--muted); }
  header #live { font-size:12px; cursor:pointer; user-select:none; margin-left:auto;
    color:var(--ok); }
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
  main { display:grid; grid-template-columns: 220px 1fr; min-height: calc(100vh - 50px); }
  nav { background:var(--rail); border-right:1px solid var(--line); padding:12px; }
  nav .group { font-size:11px; text-transform:uppercase; letter-spacing:.6px; color:var(--dim);
    margin:14px 6px 4px; }
  nav button { display:block; width:100%; text-align:left; margin:4px 0; padding:8px 10px;
    background:var(--raised); color:var(--text); border:1px solid var(--line); border-radius:6px; cursor:pointer; }
  /* No `:disabled` rule, and no `:not(:disabled)` qualifier on the hover: nav
     buttons are never disabled any more. setBusy used to grey the entire nav out
     for the duration of a fetch — which is what made switching section mid-load
     impossible, and what made the nav feel dead for the 1-2 s a section costs. */
  nav button:hover { background:var(--hover); }
  section.panel { padding:18px 22px; }
  pre#out { white-space:pre-wrap; background:var(--sunken); border:1px solid var(--line); border-radius:8px;
    padding:14px; min-height:120px; max-height:62vh; overflow:auto; }
  iframe#graph { width:100%; height:72vh; border:1px solid var(--line); border-radius:8px; background:var(--paper); }
  input, select, textarea { background:var(--sunken); color:var(--text); border:1px solid var(--line);
    border-radius:6px; padding:6px 8px; font:inherit; }
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
  .card { background:var(--sunken); border:1px solid var(--line); border-radius:10px; padding:14px 16px; }
  .card .big { font-size:26px; font-weight:600; }
  .card .lbl { font-size:12px; color:var(--muted); margin-top:2px; }
  .card .big.ok { color:var(--ok); } .card .big.bad { color:var(--bad); }
  .types { margin-top:14px; display:flex; flex-wrap:wrap; gap:8px; }
  .pill { background:var(--raised); border:1px solid var(--line); border-radius:999px; padding:3px 10px; font-size:12px; }
  .pill b { color:var(--text-strong); }
  .pill.est { color:var(--warn); border-color:#5a4a1f; }
  .pill.triclaude { color:var(--link); border-color:#2c4a75; font-weight:700; }
  .pill.tridism { color:var(--faint); }
  .pill.sr { color:#c9a6ff; border-color:#5b3f8a; background:#191227; }
  .hits { list-style:none; padding:0; margin:0; }
  .hits li { background:var(--sunken); border:1px solid var(--line); border-radius:8px; padding:10px 12px; margin:8px 0; }
  .hits .file { font-size:12px; color:var(--link); word-break:break-all; }
  .hits .snip { white-space:pre-wrap; margin-top:4px; }
  .hits .dup { color:var(--faint); }
  .snip.one { white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .skchip { display:inline-block; cursor:pointer; color:var(--link-bright); background:#12182a;
    border:1px solid #2a3550; border-radius:6px; padding:1px 7px; margin:2px 3px 0 0; }
  .skchip:hover { background:var(--hover-tint); border-color:var(--accent-edge); color:#dfeaff; }
  .statusDot { font-size:11px; margin-right:5px; vertical-align:middle; }
  .statusDot.sok { color:var(--ok-strong); }
  .statusDot.sbad { color:var(--bad-strong); }
  .statusDot.snone { color:var(--dot-idle); }
  .legitem { cursor:pointer; padding:1px 5px; border-radius:6px; }
  .legitem:hover { background:var(--hover-tint); }
  .legitem.on { background:#233; outline:1px solid var(--accent-edge); }
  .callcount { font-size:11px; color:var(--muted); margin-left:3px; }
  .callbtn { margin-left:8px; cursor:pointer; background:#12281a; border:1px solid var(--ok-edge);
    color:#9f9; border-radius:6px; font-size:11px; padding:0 7px; }
  .callbtn:hover { background:#1b3a26; color:#dfffe6; }
  .skmeta { margin:10px 0; }
  .skmeta b { color:var(--link); }
  .skbody { white-space:pre-wrap; background:var(--sunken); border:1px solid var(--line);
    border-radius:8px; padding:10px 12px; margin-top:4px; max-height:52vh; overflow:auto; }
  h2.h { margin:2px 0 12px; font-size:16px; }
  .glyph { font-weight:700; margin-right:6px; }
  .glyph.res { color:var(--ok); } .glyph.unres { color:var(--bad); }
  .glyph.watch { color:var(--warn); } .glyph.dis { color:var(--faint); }
  .legend { font-size:12px; color:var(--muted); margin:2px 0 10px; }
  .ovdone { color:var(--ok); font-weight:600; }
  .ovpend { color:var(--faint); }
  .ovspin { color:var(--warn); display:inline-block; animation: pulse 1.6s infinite; }
  /* Interactive roadmap panel (Oversee): ask + progress bar + stepper */
  .rmwrap { margin:8px 0 4px; padding:8px 10px; background:var(--inset);
    border:1px solid var(--line); border-radius:8px; }
  .rmask { margin-bottom:6px; }
  .rmint { color:var(--warn); font-size:12px; margin:2px 0 6px; }
  .rmbar { height:6px; background:var(--raised); border-radius:999px; overflow:hidden; }
  .rmfill { height:100%; background:var(--ok); border-radius:999px;
    transition:width .4s; }
  .rmstep { position:relative; padding:3px 0 3px 22px; cursor:pointer; }
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
    color:var(--muted); border:1px solid var(--line); border-radius:999px; cursor:pointer; }
  button.mini:hover { color:var(--bad); }
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
    .skel .bar, .skel .tile, .skel .line, .skel .pane { animation:none !important; }
    .rmfill { transition:none !important; }
  }
/*__CAT_CSS__*/
  /*__TASKS_CSS__*/"""

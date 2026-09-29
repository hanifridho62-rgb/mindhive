# SPDX-License-Identifier: Apache-2.0
"""The page's end of the heartbeat (blueprint P1k, MEB §1.4).

One fragment, spliced into the dashboard page AFTER `dashboard_js_actions.JS`
so that `u()` and `TOKEN` are already in scope. It is a string builder and
nothing else -- no imports, so it cannot drag the dashboard behind it.

**Why the timer is in a Worker.** Chrome applies *intensive throttling* to
`setInterval` in a tab that has been hidden for more than five minutes: the
timer fires roughly once per minute. Against a 60 s cutoff that is a coin
flip, every minute, for as long as the tab stays in the background -- and the
tab being in the background is exactly the case this feature must survive,
because a background tab is still an open one. A dedicated Worker's timers are
not on that budget. The Worker only TICKS; the fetch stays on the main thread,
where `u()` and `TOKEN` live, so the token is never copied into a second
scope.

**Why there is no `document.hidden` guard.** It is the first thing anyone
reaches for -- "don't beat when nobody is looking" -- and it inverts the
feature: pausing the beat in a hidden tab shuts the server down under a page
the user merely switched away from, and they come back to a dead panel. Hidden
is not closed. `tests/test_server_js.py` pins its absence so the instinct
cannot quietly return.

**Why `keepalive`.** A beat in flight when the tab closes is otherwise
cancelled by the browser. It costs nothing to let it land, and it makes the
last beat before a close a real data point rather than a coin toss.
"""
__all__ = ["heartbeat_script"]

#: The fragment. `%(ms)d` is the only substitution, so the shape a reader sees
#: here is byte for byte the shape the page gets.
_TEMPLATE = """(function(){
  var HEARTBEAT_MS = %(ms)d;
  var timer = null;
  function beat(){
    try {
      fetch(u('/api/heartbeat'), {method: 'POST', keepalive: true,
        headers: {'X-Hive-Token': TOKEN, 'Content-Type': 'application/json'},
        body: '{}'}).catch(function(){});
    } catch (e) {}
  }
  function fallback(){
    if (timer === null) { timer = setInterval(beat, HEARTBEAT_MS); }
  }
  beat();
  try {
    var src = 'setInterval(function(){ postMessage(0); }, ' + HEARTBEAT_MS + ');';
    var w = new Worker(URL.createObjectURL(new Blob([src],
                       {type: 'text/javascript'})));
    w.onmessage = beat;
    w.onerror = fallback;
  } catch (e) {
    fallback();
  }
})();"""


def heartbeat_script(interval_ms) -> str:
    """The heartbeat fragment, beating every *interval_ms* milliseconds.

    Refuses a non-positive interval: `setInterval(f, 0)` is a tight loop and a
    negative one never fires, and neither is a heartbeat. The number is
    rendered as an integer because it also lands in the page where a person
    reads it, and `HEARTBEAT_MS = 15000.0` invites the question of what the
    fraction is for."""
    ms = int(interval_ms)
    if ms <= 0:
        raise ValueError(
            f"the heartbeat interval must be positive milliseconds, got "
            f"{interval_ms!r}")
    return _TEMPLATE % {"ms": ms}

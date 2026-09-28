# SPDX-License-Identifier: Apache-2.0
# src/mindhive/pane.py
"""The one thing the three workspace documents agree on about being LOOKED at.

MindParallel, MindSwarm and Self-Reliant are full pages served into the shell's
`#graph` iframe, and each runs a self-scheduling poll loop of its own — 1 s, 1 s
and 1.2 s — plus a 30 s auth re-check. Leaving the section only toggles a class
on the frame, and hiding an iframe does not stop its timers: measured on the real
page, clicking away from MindParallel left the hidden frame fetching
`/api/workspace/agents` 5 times per 6 s window, and 6 more after a second click,
indefinitely, for a section nobody was looking at.

Unloading the frame stops that too, and was the first fix tried here. Skipping
the TICK is the better one, for a reason the A/B measurement made concrete: the
shell re-navigated the frame on every click even when it already held that page,
so a reader who typed a project name into MindParallel, glanced at Overview and
came back found the field empty — with the unload in place AND with it cut back
out, which is how the unload was cleared of causing it. A frame that keeps its
document keeps the half-typed sentence, the scroll position and the expanded
rows, and costs no reload on the way back.

Defined once rather than three times because it is one decision, and a decision
copied into three documents is one that drifts — the same reason `theme` exists
for the palette.
"""
from __future__ import annotations

__all__ = ["PANE_JS", "MARKER"]

MARKER = "/*__PANE__*/"

PANE_JS = """
  // True while the shell has this frame hidden. Same-origin, so a frame may read
  // its own element; `try` because a page opened on its own has no frameElement
  // at all and a cross-origin embed would throw — both of those mean "keep
  // polling", which is exactly what every one of these pages did before the
  // guard existed.
  function paneHidden(){
    try {
      const fe = window.frameElement;
      return !!(fe && fe.classList.contains('hidden'));
    } catch (e) { return false; }
  }
"""

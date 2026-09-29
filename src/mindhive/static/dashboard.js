// SPDX-License-Identifier: Apache-2.0
/*
 * MIND HIVE dashboard -- paint-signature diffing and a latched overview poll,
 * as one namespace: window.MindHive (blueprint Pillar 4, spec 10.5.3).
 *
 * Two ways in, one file. `GET /static/dashboard.js` serves it on its own, and
 * `dashboard_page.render_page` splices it, byte for byte, at the top of the
 * page's single script block. Spliced first, it shares one scope with every
 * page fragment after it, so it declares nothing outside the closure below
 * and reads no DOM until one of its functions is called.
 *
 * The poller takes from its caller what the spec's version assumed and this
 * page cannot supply. The spec fetched `/api/overview` bare, and this server
 * answers a tokenless request with 403: the caller passes its token in
 * `headers`. It painted `#overview-container` through `renderOverviewView`,
 * neither of which the page has: the caller names the `target` id and the
 * `render` function. It signed with `project_name`/`head_hash`/`event_count`,
 * none of which the overview payload carries -- the signature would have been
 * the constant "undefined_undefined_undefined", so the first paint would have
 * been the last: the signature is taken from the HTML the poll would paint.
 * And it started its own `setInterval` at load, which reads the DOM at load
 * and would poll beside the page's `refreshLive`: the caller starts it and
 * holds the returned `stop()`. The page starts none today; its own pollers
 * and latches are the core's.
 */
(() => {
    'use strict';

    // FNV-1a over what a poll would paint: a short stamp in dataset rather
    // than a second copy of the whole panel.
    const stampOf = (text) => {
        let h = 0x811c9dc5;
        for (let i = 0; i < text.length; i++) {
            h ^= text.charCodeAt(i);
            h = Math.imul(h, 0x01000193);
        }
        return (h >>> 0).toString(36) + ':' + text.length;
    };

    const MindHive = {
        // Held on the namespace so a caller can see a poll is out. The page's
        // own pollers keep their own latches; these are this file's.
        liveInFlight: false,
        catsInFlight: false,

        /**
         * Write `htmlString` into `targetElement` only when `signature` differs
         * from the one it was last painted with (kept in `dataset.paint`, which
         * stores strings -- so the comparison is on the string). A field the
         * reader was typing in is rebuilt by the write; focus goes to the new
         * one, found by id. True when it wrote.
         */
        paintInto(targetElement, htmlString, signature) {
            if (!targetElement) return false;
            const stamp = String(signature);
            if (targetElement.dataset.paint === stamp) return false;
            const active = document.activeElement;
            const keepId = active && active.id && targetElement.contains(active) ? active.id : '';
            targetElement.innerHTML = htmlString;
            targetElement.dataset.paint = stamp;
            if (keepId) {
                const rebuilt = document.getElementById(keepId);
                if (rebuilt) rebuilt.focus();
            }
            return true;
        },

        /**
         * Poll `options.url` (default `/api/overview`) every `intervalMs`,
         * render the answer with `options.render(data)` and paint it into the
         * element whose id is `options.target`, looked up at each tick. A tick
         * that finds the previous request still out is dropped, never queued.
         * The caller starts it and holds the returned `stop()`.
         */
        pollOverview(intervalMs = 2500, options = {}) {
            const { target, render } = options;
            if (!target || typeof render !== 'function') {
                throw new TypeError('pollOverview needs a target id and a render function');
            }
            const url = options.url || '/api/overview';
            const headers = Object.assign({}, options.headers);
            const tick = async () => {
                if (MindHive.liveInFlight) return;
                MindHive.liveInFlight = true;
                try {
                    const resp = await fetch(url, { headers });
                    if (!resp.ok) throw new Error('HTTP ' + resp.status);
                    const html = String(render(await resp.json()));
                    MindHive.paintInto(document.getElementById(target), html, stampOf(html));
                } catch (err) {
                    console.warn('overview poll skipped:', err);
                } finally {
                    MindHive.liveInFlight = false;
                }
            };
            const timer = setInterval(tick, intervalMs);
            return () => clearInterval(timer);
        },
    };

    window.MindHive = MindHive;
})();

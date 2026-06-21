# src/mindhive/desktop.py
from __future__ import annotations
from . import graphview


def _require_webview():
    try:
        import webview  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "desktop app needs extras: pip install -e .[app]"
        ) from e
    return webview


def _load_graph(project=None) -> dict:
    # Always rebuild from current events so the window is never stale.
    from . import graph as graph_mod
    return graph_mod.build(project=project)


def launch_app(project=None) -> None:
    """Open a native window rendering the current memory graph.

    Blocks until the window is closed. Requires the 'app' extra (pywebview).
    """
    webview = _require_webview()
    html = graphview.render_html(_load_graph(project))
    webview.create_window("MIND HIVE Graph", html=html, width=1100, height=750)
    webview.start()

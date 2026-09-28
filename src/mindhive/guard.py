# SPDX-License-Identifier: Apache-2.0
# src/mindhive/guard.py
from . import paths


def live_lock_active() -> bool:
    return paths.store_file("LIVE_LOCK").exists()


def ensure_safe(force: bool = False) -> None:
    if live_lock_active() and not force:
        raise RuntimeError(
            "V17 LIVE_LOCK active — refusing burst-CPU op to protect live trading. "
            "Use --force to override."
        )

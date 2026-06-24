# tests/test_loop_config.py
from pathlib import Path
from mindhive.loop import config


def test_defaults_when_no_file(tmp_path):
    cfg = config.load_config(tmp_path / "missing.toml")
    assert cfg.context_window == 200000
    assert cfg.smart_zone_ratio == 0.5
    assert cfg.window_hours == 5
    assert cfg.window_token_budget == 0
    assert cfg.on_cap == "pause"
    assert cfg.max_iterations == 50
    assert cfg.allowed_tools == "Write,Read,Edit,Bash(git *),Bash(npm *)"
    assert cfg.no_progress_threshold == 3


def test_overrides_from_toml(tmp_path):
    p = tmp_path / "loop.toml"
    p.write_text(
        "[model]\ncontext_window = 1000000\nsmart_zone_ratio = 0.4\n"
        "[budget]\nwindow_hours = 3\non_cap = \"stop\"\n"
        "[loop]\nmax_iterations = 10\n"
        "[breaker]\nno_progress_threshold = 2\n",
        encoding="utf-8")
    cfg = config.load_config(p)
    assert cfg.context_window == 1000000
    assert cfg.smart_zone_ratio == 0.4
    assert cfg.window_hours == 3
    assert cfg.on_cap == "stop"
    assert cfg.max_iterations == 10
    assert cfg.no_progress_threshold == 2


def test_cooldown_seconds_derived(tmp_path):
    cfg = config.load_config(tmp_path / "missing.toml")
    assert cfg.cooldown_seconds == 30 * 60

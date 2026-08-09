# tests/test_loop_config.py
from pathlib import Path
from mindhive.loop import config


def test_defaults_when_no_file(tmp_path):
    cfg = config.load_config(tmp_path / "missing.toml")
    # 1M, not the original 200000: the scalar is the FALLBACK window used when
    # a model id cannot be resolved, and every model this loop routes to except
    # Haiku 4.5 has a 1M window. A 200000 fallback rotated at 100k — ten times
    # too early on the models actually in use.
    assert cfg.context_window == 1000000
    assert cfg.smart_zone_ratio == 0.5
    # The operator's stated smart-zone ceiling, expressed directly instead of
    # being emergent from window * ratio.
    assert cfg.absolute_ceiling == 500000
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
        "absolute_ceiling = 250000\n"
        "[budget]\nwindow_hours = 3\non_cap = \"stop\"\n"
        "[loop]\nmax_iterations = 10\n"
        "[breaker]\nno_progress_threshold = 2\n",
        encoding="utf-8")
    cfg = config.load_config(p)
    assert cfg.context_window == 1000000
    assert cfg.smart_zone_ratio == 0.4
    assert cfg.absolute_ceiling == 250000
    assert cfg.window_hours == 3
    assert cfg.on_cap == "stop"
    assert cfg.max_iterations == 10
    assert cfg.no_progress_threshold == 2


def test_cooldown_seconds_derived(tmp_path):
    cfg = config.load_config(tmp_path / "missing.toml")
    assert cfg.cooldown_seconds == 30 * 60


def test_min_completion_heuristics_default_and_override(tmp_path):
    from mindhive.loop import config as cfg_mod
    assert cfg_mod.LoopConfig().min_completion_heuristics == 2
    p = tmp_path / "loop.toml"
    p.write_text("[loop]\nmin_completion_heuristics = 0\n", encoding="utf-8")
    assert cfg_mod.load_config(p).min_completion_heuristics == 0


# --- the [replenish] section (U6) --------------------------------------------

def test_a_toml_with_no_replenish_section_loads_the_defaults(tmp_path):
    p = tmp_path / "loop.toml"
    p.write_text("[loop]\nmax_iterations = 7\n", encoding="utf-8")
    cfg = config.load_config(p)
    assert cfg.max_iterations == 7
    assert cfg.replenish_enabled is True
    assert cfg.replenish_ambition == "balanced"


def test_the_replenish_section_is_read(tmp_path):
    p = tmp_path / "loop.toml"
    p.write_text('[replenish]\nenabled = false\nambition = "ambitious"\n',
                 encoding="utf-8")
    cfg = config.load_config(p)
    assert cfg.replenish_enabled is False
    assert cfg.replenish_ambition == "ambitious"


def test_a_partial_replenish_section_takes_defaults_for_the_rest(tmp_path):
    p = tmp_path / "loop.toml"
    p.write_text('[replenish]\nambition = "conservative"\n', encoding="utf-8")
    cfg = config.load_config(p)
    assert cfg.replenish_ambition == "conservative"
    assert cfg.replenish_enabled is True


def test_the_scaffold_template_round_trips_through_load_config(tmp_path):
    from mindhive.loop import scaffold
    scaffold.scaffold(tmp_path)
    cfg = config.load_config(tmp_path / "loop.toml")
    assert cfg.replenish_enabled is True
    # The template must name a level the dial actually knows, or every scaffolded
    # project starts on a silently-corrected setting.
    from mindhive import replenish
    assert replenish.resolve_ambition(cfg.replenish_ambition)[1] == ""

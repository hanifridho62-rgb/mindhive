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


# --- [replenish]: the ambition dial -------------------------------------------

def test_no_replenish_section_means_enabled_and_balanced(tmp_path):
    cfg = config.load_config(tmp_path / "missing.toml")
    assert cfg.replenish_enabled is True
    assert cfg.replenish_ambition == "balanced"
    assert cfg.replenish_max_rounds == 5


def test_the_replenish_section_is_read(tmp_path):
    p = tmp_path / "loop.toml"
    p.write_text('[replenish]\nenabled = false\nambition = "ambitious"\n'
                 'max_rounds = 2\n', encoding="utf-8")
    cfg = config.load_config(p)
    assert cfg.replenish_enabled is False
    assert cfg.replenish_ambition == "ambitious"
    assert cfg.replenish_max_rounds == 2


def test_a_partial_replenish_section_keeps_the_other_defaults(tmp_path):
    p = tmp_path / "loop.toml"
    p.write_text('[replenish]\nambition = "conservative"\n', encoding="utf-8")
    cfg = config.load_config(p)
    assert cfg.replenish_ambition == "conservative"
    assert cfg.replenish_enabled is True


def test_a_replenish_key_that_is_not_a_table_falls_back(tmp_path):
    # This file is hand-edited. A typo in one section has no business stopping a
    # run, so a scalar where a table belongs degrades to defaults.
    p = tmp_path / "loop.toml"
    p.write_text('replenish = "on"\n[loop]\nmax_iterations = 7\n',
                 encoding="utf-8")
    cfg = config.load_config(p)
    assert cfg.replenish_ambition == "balanced"
    assert cfg.max_iterations == 7        # the rest of the file still parsed


def test_the_per_setting_overrides_default_to_the_dial(tmp_path):
    # None rather than 0: the position supplies the number unless an operator
    # names one, and 0 is itself a legal min_score meaning "no floor".
    cfg = config.load_config(tmp_path / "missing.toml")
    assert cfg.replenish_min_score is None
    assert cfg.replenish_max_per_round is None
    assert cfg.replenish_dry_rounds_to_exit == 2


def test_the_per_setting_overrides_are_read(tmp_path):
    p = tmp_path / "loop.toml"
    p.write_text('[replenish]\nambition = "ambitious"\nmin_score = 40\n'
                 'max_per_round = 2\ndry_rounds_to_exit = 3\n', encoding="utf-8")
    cfg = config.load_config(p)
    assert cfg.replenish_min_score == 40
    assert cfg.replenish_max_per_round == 2
    assert cfg.replenish_dry_rounds_to_exit == 3


def test_a_zero_min_score_in_the_file_is_honoured(tmp_path):
    p = tmp_path / "loop.toml"
    p.write_text('[replenish]\nmin_score = 0\n', encoding="utf-8")
    assert config.load_config(p).replenish_min_score == 0


def test_a_quoted_number_is_coerced_rather_than_disabling_the_injector(tmp_path):
    # `ambition = "balanced"` sits in the same table, so quoting a number is the
    # natural hand-edit. Uncoerced, the string reached a `<` against a number,
    # raised TypeError inside the injector, and the loop's blanket guard turned
    # that into "honour the exit" -- the injector silently off for every run.
    p = tmp_path / "loop.toml"
    p.write_text('[replenish]\nmin_score = "40"\ndry_rounds_to_exit = "3"\n',
                 encoding="utf-8")
    cfg = config.load_config(p)
    assert cfg.replenish_min_score == 40
    assert cfg.replenish_dry_rounds_to_exit == 3


def test_an_uncoercible_value_falls_back_to_the_default(tmp_path):
    p = tmp_path / "loop.toml"
    p.write_text('[replenish]\nmin_score = "aggressive"\n', encoding="utf-8")
    assert config.load_config(p).replenish_min_score is None


def test_a_non_positive_max_per_round_falls_back_to_the_dial(tmp_path):
    # 0 would trim every survivor while the round's dry flag is computed BEFORE
    # the trim, so the round reports neither work nor dryness and the dry-round
    # exit can never fire. `enabled = false` is the off switch.
    p = tmp_path / "loop.toml"
    p.write_text('[replenish]\nmax_per_round = 0\n', encoding="utf-8")
    assert config.load_config(p).replenish_max_per_round is None


def test_an_override_beats_the_position_it_sits_beside(tmp_path):
    # The point of the escape hatch: keep `ambitious`'s seven lenses and raise its
    # bar. A dial that could only move all three settings together could not
    # express that, and the per-run cap stays the position's either way.
    from mindhive import replenish
    p = tmp_path / "loop.toml"
    p.write_text('[replenish]\nambition = "ambitious"\nmin_score = 40\n',
                 encoding="utf-8")
    cfg = config.load_config(p)
    profile = replenish.resolve_ambition(cfg.replenish_ambition).profile
    round_cfg = replenish.ReplenishConfig.for_profile(
        profile, min_score=cfg.replenish_min_score,
        max_per_round=cfg.replenish_max_per_round, bounded_run=True)
    assert round_cfg.min_score == 40
    assert round_cfg.max_per_round == 25
    assert round_cfg.max_injections_per_run == 25
    assert "innovation" in profile.lens_names

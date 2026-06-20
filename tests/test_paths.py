# tests/test_paths.py
import os
from pathlib import Path
from mindhive import paths

def test_normalize():
    assert paths.normalize("FABIO HFT V18") == "fabio-hft-v18"
    assert paths.normalize("") == "default"

def test_store_home_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    assert paths.store_home() == tmp_path

def test_project_dir_uses_env_project(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    monkeypatch.setenv("MINDHIVE_PROJECT", "Fabio_V18")
    assert paths.project_dir() == tmp_path / "fabio-v18"
    assert paths.events_dir() == tmp_path / "fabio-v18" / "events"

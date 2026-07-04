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


def test_known_projects_reads_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    (tmp_path / "autosync.json").write_text(
        '[{"project": "Fabio HFT V18", "folder": "x", "transcripts": "y"}]',
        encoding="utf-8")
    assert paths.known_projects() == {"fabio-hft-v18"}


def test_known_projects_empty_without_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    assert paths.known_projects() == set()


def test_list_namespaces_filters_non_namespace_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    (tmp_path / "fabio" / "events").mkdir(parents=True)
    (tmp_path / "orphan").mkdir()
    (tmp_path / "orphan" / "STATE.md").write_text("# STATE\n", encoding="utf-8")
    (tmp_path / "downloads").mkdir()  # incidental dir, no namespace markers
    assert paths.list_namespaces() == ["fabio", "orphan"]


def test_is_freshly_minted(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    monkeypatch.delenv("MINDHIVE_PROJECT", raising=False)
    # Registered project is never "freshly minted".
    (tmp_path / "autosync.json").write_text(
        '[{"project": "known-proj"}]', encoding="utf-8")
    assert paths.is_freshly_minted("known-proj") is False
    # An on-disk namespace is not freshly minted either.
    (tmp_path / "ondisk").mkdir()
    assert paths.is_freshly_minted("ondisk") is False
    # Unknown + not-on-disk = freshly minted.
    assert paths.is_freshly_minted("brave-brattain-28ae65") is True
    # An explicit MINDHIVE_PROJECT pin silences the signal.
    monkeypatch.setenv("MINDHIVE_PROJECT", "whatever")
    assert paths.is_freshly_minted("brave-brattain-28ae65") is False

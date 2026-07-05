"""Multi-folder sync selection: path normalization, filter files, sizes."""

import pytest

gi = pytest.importorskip("gi")

from gdrive_sync import const, rclone
from gdrive_sync.config import (Config, ensure_filters_file, minimal_paths,
                                read_user_filters, write_filters)


# --------------------------------------------------------------------------- #
# minimal_paths
# --------------------------------------------------------------------------- #

def test_minimal_paths_drops_covered_children():
    assert minimal_paths(["Work", "Work/Reports", "Photos"]) == ["Photos", "Work"]


def test_minimal_paths_normalizes_and_dedups():
    assert minimal_paths(["/Work/", "Work", "  ", ""]) == ["Work"]


def test_minimal_paths_keeps_similar_prefixes():
    # "Work2" is NOT inside "Work"
    assert minimal_paths(["Work", "Work2"]) == ["Work", "Work2"]


# --------------------------------------------------------------------------- #
# Filter files with include rules
# --------------------------------------------------------------------------- #

def test_write_filters_without_selection_has_no_includes(tmp_path):
    f = tmp_path / "filters.txt"
    write_filters(["*.iso"], f)
    lines = f.read_text().splitlines()
    assert not [l for l in lines if l.startswith("+ ")]
    assert "- **" not in lines
    assert "- *.iso" in lines


def test_write_filters_with_selection(tmp_path):
    f = tmp_path / "filters.txt"
    write_filters(["*.iso"], f, include_dirs=["Photos", "Work/Reports"])
    lines = f.read_text().splitlines()
    assert "+ /Photos/**" in lines
    assert "+ /Work/Reports/**" in lines
    assert lines[-1] == "- **"
    # excludes must come before the include rules (first match wins in rclone)
    assert lines.index("- *.iso") < lines.index("+ /Photos/**")
    for pattern in const.INTERNAL_EXCLUDES:
        assert lines.index(f"- {pattern}") < lines.index("+ /Photos/**")


def test_write_filters_escapes_glob_chars(tmp_path):
    f = tmp_path / "filters.txt"
    write_filters([], f, include_dirs=["My [2024] *stuff*"])
    assert "+ /My \\[2024\\] \\*stuff\\*/**" in f.read_text()


def test_write_filters_blank_selection_syncs_everything(tmp_path):
    f = tmp_path / "filters.txt"
    write_filters([], f, include_dirs=["  ", "/"])
    assert "- **" not in f.read_text().splitlines()


def test_read_user_filters_ignores_include_rules(tmp_path):
    f = tmp_path / "filters.txt"
    write_filters(["*.iso", "cache/**"], f, include_dirs=["Photos"])
    assert read_user_filters(f) == ["*.iso", "cache/**"]


def test_rewrite_preserves_selection(tmp_path):
    """Editing exclusions from the preferences must not drop the selection."""
    f = tmp_path / "filters.txt"
    write_filters(["*.iso"], f, include_dirs=["Photos"])
    patterns = read_user_filters(f) + ["*.tmp2"]
    write_filters(patterns, f, include_dirs=["Photos"])
    text = f.read_text()
    assert "+ /Photos/**" in text and "- *.tmp2" in text


# --------------------------------------------------------------------------- #
# AccountConfig.sync_folders
# --------------------------------------------------------------------------- #

@pytest.fixture
def config(monkeypatch):
    monkeypatch.setattr("gdrive_sync.config._lookup_schema", lambda _id: None)
    return Config()


def test_sync_folders_default_empty(config):
    assert config.account("account1").sync_folders == []


def test_sync_folders_roundtrip_minimizes(config):
    acc = config.account("account1")
    acc.sync_folders = ["Work/Reports", "Work", "Photos"]
    assert acc.sync_folders == ["Photos", "Work"]


# --------------------------------------------------------------------------- #
# rclone size parsing
# --------------------------------------------------------------------------- #

def test_parse_size_output():
    size = rclone.parse_size_output('{"count":123,"bytes":4567890,"sizeless":0}')
    assert size.bytes == 4567890
    assert size.count == 123


def test_parse_size_output_empty_folder():
    size = rclone.parse_size_output('{"count":0,"bytes":0}')
    assert size.bytes == 0 and size.count == 0


def test_format_size():
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gdrive_sync.gui.drive_tree import format_size
    assert format_size(0) == "0 B"
    assert format_size(999) == "999 B"
    assert format_size(1400) == "1.4 kB"
    assert format_size(2_500_000) == "2.5 MB"
    assert format_size(1_400_000_000) == "1.4 GB"


# --------------------------------------------------------------------------- #
# ensure_filters_file must honor the folder selection (regression: a missing
# filters file was regenerated without include rules, widening the sync to
# the whole Drive and blocking every repair)
# --------------------------------------------------------------------------- #

def test_ensure_filters_file_regenerates_with_selection(tmp_path):
    f = tmp_path / "filters.txt"
    ensure_filters_file(f, include_dirs=["Documenti", "Notes"])
    lines = f.read_text().splitlines()
    assert "+ /Documenti/**" in lines
    assert "+ /Notes/**" in lines
    assert lines[-1] == "- **"


def test_ensure_filters_file_does_not_touch_existing(tmp_path):
    f = tmp_path / "filters.txt"
    write_filters(["*.iso"], f, include_dirs=["Photos"])
    before = f.read_text()
    ensure_filters_file(f, include_dirs=["Documenti"])  # must be a no-op
    assert f.read_text() == before


# --------------------------------------------------------------------------- #
# Tray: aggregated state (worst state wins)
# --------------------------------------------------------------------------- #

def test_tray_aggregate_state():
    gi.require_version("Gtk", "4.0")
    from gdrive_sync.daemon.tray import aggregate_state
    assert aggregate_state([]) == "idle"
    assert aggregate_state(["idle", "idle"]) == "idle"
    assert aggregate_state(["idle", "syncing"]) == "syncing"
    assert aggregate_state(["resyncing"]) == "syncing"
    assert aggregate_state(["syncing", "error"]) == "error"
    assert aggregate_state(["idle", "needs_resync"]) == "error"
    assert aggregate_state(["paused", "paused"]) == "paused"
    assert aggregate_state(["paused", "idle"]) == "idle"
    assert aggregate_state(["offline", "paused"]) == "offline"

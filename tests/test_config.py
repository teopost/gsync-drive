"""Account management logic (runs on the in-memory fallback, no schema needed)."""

import pytest

gi = pytest.importorskip("gi")

from gdrive_sync import const
from gdrive_sync.config import Config


@pytest.fixture
def config(monkeypatch):
    # Force the schema-less fallback even where schemas are installed.
    monkeypatch.setattr("gdrive_sync.config._lookup_schema", lambda _id: None)
    return Config()


def test_reserve_publish_remove(config):
    assert config.account_ids == []
    aid = config.reserve_account_id()
    assert aid == "account1"
    # reserved but not listed yet
    assert config.account_ids == []
    config.publish_account(aid)
    assert config.account_ids == ["account1"]

    aid2 = config.reserve_account_id()
    assert aid2 == "account2"
    config.publish_account(aid2)
    config.remove_account("account1")
    assert config.account_ids == ["account2"]


def test_publish_is_idempotent(config):
    config.publish_account("account1")
    config.publish_account("account1")
    assert config.account_ids == ["account1"]


def test_account_defaults(config):
    acc = config.account("account7")
    assert acc.display_name == "account7"       # falls back to the id
    assert acc.local_dir == const.DEFAULT_LOCAL_DIR
    assert acc.remote == "gdrive-sync-account7:"
    assert acc.remote_name == "gdrive-sync-account7"
    assert acc.sync_interval == const.DEFAULT_SYNC_INTERVAL
    assert acc.filters_file.name == "filters-account7.txt"
    assert acc.workdir.name == "account7"
    assert acc.status_file.name == "status-account7.json"


def test_account_overrides(config):
    acc = config.account("account1")
    acc.display_name = "Lavoro"
    acc.local_dir = "/tmp/x"
    acc.remote = "gdrive-sync:"
    assert acc.display_name == "Lavoro"
    assert str(acc.local_dir) == "/tmp/x"
    assert acc.remote_name == "gdrive-sync"


def test_reset_clears_account(config):
    acc = config.account("account1")
    acc.display_name = "X"
    acc.reset()
    assert acc.display_name == "account1"


# --------------------------------------------------------------------------- #
# On-disk state cleanup (account ids get reused)
# --------------------------------------------------------------------------- #

@pytest.fixture
def state_dirs(monkeypatch, tmp_path):
    monkeypatch.setattr(const, "BISYNC_WORKDIR", tmp_path / "bisync")
    monkeypatch.setattr(const, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(const, "CONFIG_DIR", tmp_path / "config")
    return tmp_path


def _plant_leftovers(acc):
    acc.workdir.mkdir(parents=True, exist_ok=True)
    (acc.workdir / "path1.lst").write_text("old listing")
    acc.status_file.parent.mkdir(parents=True, exist_ok=True)
    acc.status_file.write_text('{"last_sync_time": 12345}')
    acc.sync_log_file.write_text("old log")
    acc.filters_file.parent.mkdir(parents=True, exist_ok=True)
    acc.filters_file.write_text("- *.iso\n")
    pathlib_md5 = acc.filters_file.parent / (acc.filters_file.name + ".md5")
    pathlib_md5.write_text("abc")


def test_purge_state_removes_leftovers(config, state_dirs):
    acc = config.account("account1")
    _plant_leftovers(acc)
    acc.purge_state()
    assert not acc.workdir.exists()
    assert not acc.status_file.exists()
    assert not acc.sync_log_file.exists()
    assert acc.filters_file.exists()  # kept unless include_filters=True
    acc.purge_state(include_filters=True)
    assert not acc.filters_file.exists()
    assert not (acc.filters_file.parent / (acc.filters_file.name + ".md5")).exists()


def test_purge_state_on_missing_files_is_noop(config, state_dirs):
    config.account("account9").purge_state(include_filters=True)  # must not raise


def test_remove_account_purges_state(config, state_dirs):
    config.publish_account("account1")
    acc = config.account("account1")
    _plant_leftovers(acc)
    config.remove_account("account1")
    assert not acc.workdir.exists()
    assert not acc.status_file.exists()
    assert not acc.filters_file.exists()


def test_schema_fallback_is_flagged(config):
    """The in-memory fallback must be detectable: GUIs/daemons must not treat
    it as a real empty configuration (regression: a schema hiccup during a
    package upgrade opened the wizard, which purged a real account)."""
    assert config.schema_ok is False

"""State-machine tests with a mocked rclone runner and a running GLib loop."""

import threading

import pytest

gi = pytest.importorskip("gi")
from gi.repository import GLib

from gdrive_sync import rclone
from gdrive_sync.daemon.engine import Engine, State
from gdrive_sync.rclone import Outcome, RcloneVersion, SyncResult


class FakeAccount:
    settings = None

    def __init__(self, tmp_path):
        self.id = "test1"
        self.display_name = "Test"
        self.local_dir = tmp_path / "local"
        self.sync_interval = 3600
        self.bandwidth_limit = ""
        self.max_delete = 25
        self.dry_run = False
        self.root_folder_id = ""
        self.sidebar_bookmark = True
        self.remote = str(tmp_path / "remote")  # path (not ':') -> no remote check
        self.sync_folders = []
        self.filters_file = tmp_path / "filters.txt"
        self.workdir = tmp_path / "work"
        self.sync_log_file = tmp_path / "run.log"
        self.status_file = tmp_path / "status.json"

    def connect_changed(self, cb):
        pass


class FakeRun:
    def __init__(self, result):
        self._result = result
        self.cancelled = False

    def cancel(self):
        self.cancelled = True

    def wait(self, timeout=0):
        return self._result


@pytest.fixture
def engine(tmp_path, monkeypatch):
    """Engine wired to a fake rclone; results are queued per-run."""
    results: list[SyncResult] = []

    def fake_run_ctor(cmd):
        return FakeRun(results.pop(0) if results else SyncResult(Outcome.SUCCESS, 0))

    monkeypatch.setattr(rclone, "BisyncRun", fake_run_ctor)
    monkeypatch.setattr(rclone, "remote_exists", lambda name=None: True)

    account = FakeAccount(tmp_path)
    account.local_dir.mkdir()
    (tmp_path / "remote").mkdir()

    eng = Engine(account, RcloneVersion(1, 74))
    eng._queue = results  # test hook
    # Pretend a previous successful sync so request_sync doesn't force --resync
    eng.last_sync_time = 1
    return eng


def pump(seconds: float = 0.3):
    """Run the default main loop long enough for idle/thread callbacks."""
    loop = GLib.MainLoop()
    GLib.timeout_add(int(seconds * 1000), loop.quit)
    loop.run()


def wait_for_state(eng, state, timeout=5.0):
    deadline = threading.Event()
    GLib.timeout_add(int(timeout * 1000), deadline.set)
    while eng.state is not state and not deadline.is_set():
        pump(0.05)
    assert eng.state is state, f"expected {state}, got {eng.state}"


def test_success_flow(engine):
    states = []
    engine.on_state_changed = states.append
    engine.start()
    wait_for_state(engine, State.IDLE)
    assert State.SYNCING in states
    assert engine.last_sync_time > 1


def test_first_run_uses_resync(engine):
    """A brand-new account (never synced) starts with an automatic --resync."""
    engine.last_sync_time = 0
    states = []
    engine.on_state_changed = states.append
    notifications = []
    engine.on_notify = lambda kind, t, b: notifications.append(kind)
    engine.start()
    wait_for_state(engine, State.IDLE)
    assert State.RESYNCING in states       # not plain SYNCING
    assert "first-sync" in notifications


def test_persisted_last_sync_time(engine, tmp_path):
    engine.start()
    wait_for_state(engine, State.IDLE)
    reloaded = Engine(engine.account, RcloneVersion(1, 74))
    assert reloaded.last_sync_time == engine.last_sync_time
    reloaded.shutdown()


def test_needs_resync_flow(engine):
    notifications = []
    engine.on_notify = lambda kind, t, b: notifications.append(kind)
    engine._queue.append(SyncResult(Outcome.NEEDS_RESYNC, 7, stderr="Must run --resync"))
    engine.start()
    wait_for_state(engine, State.NEEDS_RESYNC)

    engine.request_sync("test")
    assert engine.state is State.NEEDS_RESYNC
    assert "needs-resync" in notifications

    engine.request_resync()
    wait_for_state(engine, State.IDLE)


def test_resync_allowed_from_idle(engine):
    # Needed when the Drive folder selection changes: filters change and
    # bisync requires a user-approved --resync to realign.
    engine.start()
    wait_for_state(engine, State.IDLE)
    engine.request_resync()
    wait_for_state(engine, State.IDLE)


def test_resync_rejected_while_paused(engine):
    engine.start()
    wait_for_state(engine, State.IDLE)
    engine.pause()
    with pytest.raises(RuntimeError):
        engine.request_resync()


def test_cancelled_run_returns_to_idle(engine):
    engine._queue.append(SyncResult(Outcome.CANCELLED, -9, stderr="cancelled by user"))
    errors = []
    engine.on_notify = lambda kind, t, b: errors.append(kind)
    engine.start()
    wait_for_state(engine, State.IDLE)
    assert errors == []          # no notification for a user cancel
    assert engine._timer_id != 0  # scheduling continues


def test_transient_error_retries_with_backoff(engine):
    engine._queue.append(SyncResult(Outcome.TRANSIENT, 5, stderr="temporary"))
    engine.start()
    wait_for_state(engine, State.ERROR)
    assert engine._retry_index == 1
    assert engine._timer_id != 0  # retry armed


def test_pause_resume(engine):
    engine.start()
    wait_for_state(engine, State.IDLE)
    engine.pause()
    assert engine.state is State.PAUSED
    engine.request_sync("ignored-while-paused")
    assert engine.state is State.PAUSED
    engine.resume()
    wait_for_state(engine, State.IDLE)


def test_dirty_rerun_after_sync(engine, monkeypatch):
    """A change arriving mid-sync triggers exactly one follow-up run."""
    run_count = 0
    real_start = engine._start_run

    def counting_start(resync):
        nonlocal run_count
        run_count += 1
        real_start(resync)

    monkeypatch.setattr(engine, "_start_run", counting_start)
    engine.start()
    engine._dirty = True  # simulate a change during the first run
    wait_for_state(engine, State.IDLE)
    pump(0.3)
    wait_for_state(engine, State.IDLE)
    assert run_count >= 2


def test_empty_prior_listing_auto_resyncs(engine):
    """A brand-new empty account must self-heal instead of demanding a manual
    repair: an empty prior listing means nothing is tracked, so an automatic
    resync cannot delete anything."""
    states = []
    engine.on_state_changed = states.append
    notifications = []
    engine.on_notify = lambda kind, t, b: notifications.append(kind)
    engine._queue.append(SyncResult(
        Outcome.NEEDS_RESYNC, 7,
        stderr="Bisync critical error: empty prior Path1 listing: x.path1.lst"))
    engine.start()
    wait_for_state(engine, State.IDLE)
    assert State.RESYNCING in states          # auto-resync ran
    assert "needs-resync" not in notifications


def test_empty_listing_auto_resync_does_not_loop(engine):
    """If the auto-resync itself ends with another empty-listing abort, the
    engine must fall back to NEEDS_RESYNC instead of resyncing forever."""
    err = "Bisync critical error: empty prior Path1 listing: x.path1.lst"
    engine._queue.append(SyncResult(Outcome.NEEDS_RESYNC, 7, stderr=err))
    engine._queue.append(SyncResult(Outcome.NEEDS_RESYNC, 7, stderr=err))
    engine.start()
    wait_for_state(engine, State.NEEDS_RESYNC)

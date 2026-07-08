import subprocess

import pytest

from gdrive_sync import const, rclone
from gdrive_sync.rclone import Outcome, RcloneVersion


# --------------------------------------------------------------------------- #
# Version parsing / feature gates
# --------------------------------------------------------------------------- #

def test_parse_version_ubuntu_160():
    v = rclone.parse_version("rclone v1.60.1-DEV\n- os/version: ubuntu 26.04 (64 bit)\n")
    assert (v.major, v.minor) == (1, 60)
    assert v.has_bisync
    assert not v.has_resilient_flags
    assert not v.has_recover_flags
    assert not v.is_recommended


def test_parse_version_upstream_171():
    v = rclone.parse_version("rclone v1.71.0\n")
    assert v.has_resilient_flags and v.has_recover_flags and v.is_recommended


def test_parse_version_garbage():
    with pytest.raises(ValueError):
        rclone.parse_version("command not found")


# --------------------------------------------------------------------------- #
# authorize output parsing
# --------------------------------------------------------------------------- #

AUTHORIZE_OUT = """\
2026/07/04 12:00:00 NOTICE: If your browser doesn't open automatically go to the following link: http://127.0.0.1:53682/auth?state=xyz
2026/07/04 12:00:05 NOTICE: Got code
Paste the following into your remote machine --->
{"access_token":"ya29.AAA","token_type":"Bearer","refresh_token":"1//BBB","expiry":"2026-07-04T13:00:00.0+02:00"}
<---End paste
"""


def test_parse_authorize_output():
    token = rclone.parse_authorize_output(AUTHORIZE_OUT)
    import json
    data = json.loads(token)
    assert data["access_token"] == "ya29.AAA"
    assert data["refresh_token"] == "1//BBB"


def test_parse_authorize_output_no_token():
    with pytest.raises(rclone.AuthorizationError):
        rclone.parse_authorize_output("NOTICE: nothing here\n{not json}\n")


# --------------------------------------------------------------------------- #
# Drive folder listing (lsf) parsing
# --------------------------------------------------------------------------- #

LSF_OUT = """\
1AbCdEfGh123;Documenti/
9ZyXwVu456;Foto vacanze/
5MnOpQr789;lavoro/
"""


def test_parse_lsf_folders():
    folders = rclone.parse_lsf_folders(LSF_OUT)
    assert [f.name for f in folders] == ["Documenti", "Foto vacanze", "lavoro"]
    assert folders[0].folder_id == "1AbCdEfGh123"


def test_parse_lsf_folders_empty_and_garbage():
    assert rclone.parse_lsf_folders("") == []
    assert rclone.parse_lsf_folders("no separator here\n;\n") == []


# --------------------------------------------------------------------------- #
# Transfer progress parsing (--stats log lines)
# --------------------------------------------------------------------------- #

STATS_LOG = """\
2026/07/04 12:00:02 INFO  :
Transferred:   	  512 KiB / 10.5 MiB, 5%, 250 KiB/s, ETA 40s
Transferred:            3 / 42, 7%
2026/07/04 12:00:04 INFO  :
Transferred:   	    5 MiB / 10.5 MiB, 48%, 2.5 MiB/s, ETA 2s
Transferred:           20 / 42, 48%
"""


def test_parse_progress_latest_wins():
    assert rclone.parse_progress(STATS_LOG) == 0.48


def test_parse_progress_none_without_stats():
    assert rclone.parse_progress("INFO : Synching Path1 ...") is None
    assert rclone.parse_progress("") is None


def test_parse_progress_caps_at_100():
    assert rclone.parse_progress("Transferred: 1 / 1, 100%") == 1.0


# --------------------------------------------------------------------------- #
# bisync command builder (rclone 1.60 vs 1.66)
# --------------------------------------------------------------------------- #

V160 = RcloneVersion(1, 60)
V171 = RcloneVersion(1, 71)


def test_build_cmd_160_baseline():
    cmd = rclone.build_bisync_cmd("/home/x/GoogleDrive", version=V160)
    assert cmd[:4] == ["rclone", "bisync", "/home/x/GoogleDrive", const.REMOTE]
    assert "--filters-file" in cmd and "--workdir" in cmd
    assert "--max-delete" in cmd
    # Flags newer than 1.60 must not appear
    for flag in ("--resilient", "--recover", "--max-lock", "--create-empty-src-dirs"):
        assert flag not in cmd


def test_build_cmd_171_gets_gated_flags():
    cmd = rclone.build_bisync_cmd("/d", version=V171)
    for flag in ("--resilient", "--recover", "--max-lock", "--create-empty-src-dirs"):
        assert flag in cmd


def test_build_cmd_resync_excludes_max_delete():
    cmd = rclone.build_bisync_cmd("/d", version=V160, resync=True)
    assert "--resync" in cmd
    assert "--max-delete" not in cmd


def test_build_cmd_options():
    cmd = rclone.build_bisync_cmd("/d", version=V160, dry_run=True, bwlimit="1M")
    assert "--dry-run" in cmd
    assert cmd[cmd.index("--bwlimit") + 1] == "1M"
    assert "--stats" not in cmd


def test_build_cmd_stats():
    cmd = rclone.build_bisync_cmd("/d", version=V160, stats="2s")
    assert cmd[cmd.index("--stats") + 1] == "2s"
    assert "--stats-log-level" in cmd


# --------------------------------------------------------------------------- #
# Exit-code / output classification
# --------------------------------------------------------------------------- #

RESYNC_LOG = """\
2026/07/04 12:00:00 INFO  : Synching Path1 "/home/x/GoogleDrive/" with Path2 "gdrive-sync:/"
2026/07/04 12:00:01 ERROR : Bisync critical error: cannot find prior Path1 or Path2 listings, likely due to critical error on prior run
2026/07/04 12:00:01 ERROR : Bisync aborted. Must run --resync to recover.
"""

LOCK_LOG = """\
2026/07/04 12:00:00 ERROR : Bisync critical error: prior lock file found: /home/x/.cache/rclone/bisync/path1..path2.lck
2026/07/04 12:00:00 ERROR : Bisync aborted. Must run --resync to recover.
"""

CONFLICT_LOG = """\
2026/07/04 12:00:03 NOTICE: - WARNING   New or changed in both paths      - doc.txt
2026/07/04 12:00:03 NOTICE: - Path1     Renaming Path1 copy               - /home/x/GoogleDrive/doc.txt..path1
2026/07/04 12:00:04 NOTICE: - Path2     Renaming Path2 copy               - gdrive-sync:/doc.txt..path2
2026/07/04 12:00:05 INFO  : Bisync successful
"""


@pytest.mark.parametrize("code,stderr,log_tail,expected", [
    (0, "", "Bisync successful", Outcome.SUCCESS),
    (9, "", "", Outcome.SUCCESS),
    (1, "usage error", "", Outcome.FATAL),
    (5, "temporary error", "", Outcome.TRANSIENT),
    (7, "", RESYNC_LOG, Outcome.NEEDS_RESYNC),
    (7, "", LOCK_LOG, Outcome.LOCKED),  # lock takes precedence over resync text
    (1, "", LOCK_LOG, Outcome.LOCKED),  # some rclone versions exit 1 on the lock abort
    (5, "", LOCK_LOG, Outcome.LOCKED),  # the lock pattern wins at any exit code
    (1, "", RESYNC_LOG, Outcome.NEEDS_RESYNC),
    (7, "some other fatal thing", "", Outcome.FATAL),
    (2, "", RESYNC_LOG, Outcome.NEEDS_RESYNC),
    (8, "transfer limit", "", Outcome.TRANSIENT),
])
def test_classify(code, stderr, log_tail, expected):
    assert rclone.classify_result(code, stderr, log_tail) is expected


def test_conflict_hint_detected(tmp_path):
    """run_bisync reads the log written by the subprocess (a stand-in for rclone)."""
    logf = tmp_path / "run.log"
    src = tmp_path / "conflict-log.txt"
    src.write_text(CONFLICT_LOG)
    # The python one-liner plays rclone: it writes the log file and exits 0.
    # The extra --log-file argument is what run_bisync parses to find the log.
    cmd = ["python3", "-c",
           f"import shutil; shutil.copy({str(src)!r}, {str(logf)!r})",
           "--log-file", str(logf)]
    result = rclone.run_bisync(cmd)
    assert result.ok
    assert result.conflicts_hinted


def test_bisync_run_cancel(tmp_path):
    run = rclone.BisyncRun(["sleep", "30"])
    run.cancel()
    result = run.wait(timeout=10)
    assert result.outcome is Outcome.CANCELLED


# --------------------------------------------------------------------------- #
# Classification: network failures during bisync (regression: a DNS outage
# was classified as NEEDS_RESYNC because rclone prints "Bisync aborted" on
# every critical error)
# --------------------------------------------------------------------------- #

def test_dns_failure_is_transient():
    log_tail = (
        'ERROR : critical error: couldn\'t fetch token: Post '
        '"https://oauth2.googleapis.com/token": dial tcp: lookup '
        'oauth2.googleapis.com on 127.0.0.53:53: server misbehaving\n'
        'ERROR : Bisync aborted. Error is retryable without --resync '
        'due to --resilient mode.\n'
        'NOTICE: Failed to bisync: bisync aborted')
    assert rclone.classify_result(7, "", log_tail) is Outcome.TRANSIENT


def test_connection_reset_is_transient():
    assert rclone.classify_result(
        7, "", "dial tcp 142.250.0.1:443: connection refused\nBisync aborted"
    ) is Outcome.TRANSIENT


def test_filters_changed_still_needs_resync():
    assert rclone.classify_result(
        7, "", "filters file has changed (must run --resync)\nBisync aborted"
    ) is Outcome.NEEDS_RESYNC


def test_unknown_critical_abort_is_fatal_not_resync():
    assert rclone.classify_result(
        7, "", "some unexpected condition\nBisync aborted"
    ) is Outcome.FATAL


# --------------------------------------------------------------------------- #
# Classification: a bisync interrupted by a shutdown leaves its lock file
# behind; the startup sync then aborts on "prior lock file found" with exit
# code 1, not 7 (regression: exit 1 was mapped to FATAL before any pattern
# check, so the engine never cleared the stale lock and the account stayed
# in error until a manual sync)
# --------------------------------------------------------------------------- #

def test_lock_abort_exit_1_is_locked():
    log_tail = (
        "2026/07/08 22:19:56 INFO  : /home/x/.local/state/gdrive-sync/bisync/"
        "account1/path1..path2.lck: Valid lock file found. Expires at "
        "2026-07-08 22:23:47 +0200 CEST. (3m51s from now)\n"
        "Errors:                 1 (retrying may help)\n"
        "2026/07/08 22:19:56 NOTICE: Failed to bisync: prior lock file found: "
        "/home/x/.local/state/gdrive-sync/bisync/account1/path1..path2.lck"
    )
    assert rclone.classify_result(1, "", log_tail) is Outcome.LOCKED


def test_network_abort_exit_1_is_transient():
    assert rclone.classify_result(
        1, "", "dial tcp: lookup drive.google.com: no such host\nBisync aborted"
    ) is Outcome.TRANSIENT

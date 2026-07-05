from pathlib import Path

from gdrive_sync import conflicts


def make(p: Path, content: str = "x") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return p


def test_scan_old_style(tmp_path):
    make(tmp_path / "doc.txt..path1", "local")
    make(tmp_path / "doc.txt..path2", "remote")
    make(tmp_path / "normal.txt")
    found = conflicts.scan(tmp_path)
    assert len(found) == 1
    c = found[0]
    assert c.base_path == tmp_path / "doc.txt"
    assert c.local_variant.name == "doc.txt..path1"
    assert c.remote_variant.name == "doc.txt..path2"


def test_scan_new_style_and_subdirs(tmp_path):
    make(tmp_path / "sub" / "a.ods.conflict1")
    make(tmp_path / "sub" / "a.ods.conflict2")
    found = conflicts.scan(tmp_path)
    assert len(found) == 1
    assert found[0].base_path == tmp_path / "sub" / "a.ods"


def test_scan_empty_and_missing(tmp_path):
    assert conflicts.scan(tmp_path) == []
    assert conflicts.scan(tmp_path / "nope") == []


def test_resolve_keep_local(tmp_path):
    make(tmp_path / "f.txt", "old-base")
    local = make(tmp_path / "f.txt..path1", "local")
    remote = make(tmp_path / "f.txt..path2", "remote")
    c = conflicts.Conflict(tmp_path / "f.txt", local, remote)
    conflicts.resolve_keep(c, "local")
    assert (tmp_path / "f.txt").read_text() == "local"
    assert not local.exists() and not remote.exists()


def test_resolve_keep_remote(tmp_path):
    local = make(tmp_path / "f.txt..path1", "local")
    remote = make(tmp_path / "f.txt..path2", "remote")
    c = conflicts.Conflict(tmp_path / "f.txt", local, remote)
    conflicts.resolve_keep(c, "remote")
    assert (tmp_path / "f.txt").read_text() == "remote"


def test_resolve_keep_both(tmp_path):
    local = make(tmp_path / "f.txt..path1", "local")
    remote = make(tmp_path / "f.txt..path2", "remote")
    c = conflicts.Conflict(tmp_path / "f.txt", local, remote)
    conflicts.resolve_keep(c, "both")
    assert (tmp_path / "f (local).txt").read_text() == "local"
    assert (tmp_path / "f (remote).txt").read_text() == "remote"


def test_resolve_one_sided_conflict(tmp_path):
    local = make(tmp_path / "f.txt..path1", "local")
    c = conflicts.Conflict(tmp_path / "f.txt", local, None)
    conflicts.resolve_keep(c, "local")
    assert (tmp_path / "f.txt").read_text() == "local"

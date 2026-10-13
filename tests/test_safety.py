"""Regression tests for filesystem identity, scan failures and safe removal."""

import argparse
import hashlib
import os
import sys

import pytest

from sha256_duplicate_finder import (
    DuplicateScannerEngine,
    force_delete_file,
    move_to_trash,
    parse_arguments,
    remove_verified_duplicate,
    run_cli,
)


def _scan(*paths, follow_symlinks=False):
    engine = DuplicateScannerEngine(
        roots=list(paths), min_size_bytes=0, workers=2,
        follow_symlinks=follow_symlinks,
    )
    engine.run()
    assert engine.error is None
    return engine


def test_overlap_roots_do_not_duplicate_file_entries(tmp_path):
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "single.txt").write_bytes(b"only one physical file")
    engine = _scan(tmp_path, nested, tmp_path)
    assert engine.duplicate_groups == []


def test_hardlinks_are_not_counted_as_independent_copies(tmp_path):
    original = tmp_path / "original.txt"
    original.write_bytes(b"same content in two inodes")
    alias = tmp_path / "alias.txt"
    try:
        os.link(original, alias)
    except (OSError, NotImplementedError):
        pytest.skip("Hardlinks are not supported on this filesystem")
    copy = tmp_path / "copy.txt"
    copy.write_bytes(original.read_bytes())
    engine = _scan(tmp_path)
    assert len(engine.duplicate_groups) == 1
    assert len(engine.duplicate_groups[0]) == 2
    assert {p.name for p in engine.duplicate_groups[0]} & {"original.txt", "alias.txt"}


def test_symlink_directory_cycle_terminates(tmp_path):
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "one.bin").write_bytes(b"test data")
    cycle = nested / "cycle"
    try:
        cycle.symlink_to(tmp_path, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Directory symlinks are not supported")
    engine = _scan(tmp_path, follow_symlinks=True)
    assert engine.duplicate_groups == []


def _meta(keeper, target):
    blob = keeper.read_bytes()
    digest = hashlib.sha256(blob).hexdigest()
    return {
        keeper: (len(blob), keeper.stat().st_mtime, digest),
        target: (len(blob), target.stat().st_mtime, digest),
    }


def test_verified_deletion_accepts_unchanged_distinct_files(tmp_path):
    keeper = tmp_path / "keep"
    target = tmp_path / "remove"
    keeper.write_bytes(b"matching contents")
    target.write_bytes(keeper.read_bytes())
    metadata = _meta(keeper, target)
    ok, message = remove_verified_duplicate(target, keeper, metadata)
    assert ok, message
    assert keeper.exists()
    assert not target.exists()


@pytest.mark.parametrize("changed", ["target", "keeper"])
def test_verified_deletion_refuses_changed_content(tmp_path, changed):
    keeper = tmp_path / "keep"
    target = tmp_path / "remove"
    keeper.write_bytes(b"matching contents")
    target.write_bytes(keeper.read_bytes())
    metadata = _meta(keeper, target)
    (keeper if changed == "keeper" else target).write_bytes(b"modified contents")
    ok, _ = remove_verified_duplicate(target, keeper, metadata)
    assert not ok
    assert keeper.exists()
    assert target.exists()


def test_verified_deletion_refuses_symlink(tmp_path):
    keeper = tmp_path / "keep"
    keeper.write_bytes(b"contents")
    link = tmp_path / "link"
    try:
        link.symlink_to(keeper)
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks are not supported")
    metadata = _meta(keeper, link)
    ok, _ = remove_verified_duplicate(link, keeper, metadata)
    assert not ok
    assert link.is_symlink()
    assert keeper.exists()
    ok, _ = force_delete_file(link)
    assert not ok


def test_trash_does_not_chmod_files(monkeypatch, tmp_path):
    import sha256_duplicate_finder as module

    target = tmp_path / "test.txt"
    target.write_text("data")
    calls = []

    class DummyTrash:
        @staticmethod
        def send2trash(path):
            calls.append(path)

    monkeypatch.setattr(module, "HAS_SEND2TRASH", True)
    monkeypatch.setattr(module, "send2trash", DummyTrash)
    monkeypatch.setattr(module.os, "chmod", lambda *_: pytest.fail("chmod should not be called"))
    ok, _ = move_to_trash(target)
    assert ok
    assert calls == [str(target)]


def test_cli_returns_failure_when_scan_fails(monkeypatch, tmp_path):
    def failed_run(self):
        self.error = "simulated filesystem failure"

    monkeypatch.setattr(DuplicateScannerEngine, "run", failed_run)
    args = argparse.Namespace(
        paths=[str(tmp_path)], hdd_mode=True, workers=None,
        min_size_kb=0.0, retention="oldest", include_hidden=False,
        follow_symlinks=False, dry_run=True, delete=False, trash=False,
        export_csv="", export_json="", yes=False,
    )
    assert run_cli(args) == 1


@pytest.mark.parametrize("argv", [
    ["--min-size-kb", "-1"],
    ["--min-size-kb", "nan"],
    ["--min-size-kb", "inf"],
    ["--workers", "0"],
    ["--trash", "--delete"],
    ["--gui", "--cli"],
])
def test_cli_rejects_invalid_options(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["sha256_duplicate_finder"] + argv)
    with pytest.raises(SystemExit) as ex:
        parse_arguments()
    assert ex.value.code == 2

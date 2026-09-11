"""
Unit and integration tests for DuplicateScannerEngine and CLI utilities.
"""

import os
import tempfile
import time
from pathlib import Path
import pytest

from sha256_duplicate_finder import (
    DuplicateScannerEngine,
    export_csv_report,
    export_json_report,
    force_delete_file,
    fmt_size,
    parse_arguments,
)


@pytest.fixture
def temp_test_environment():
    """Create a temporary directory hierarchy with unique and duplicate files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        dir_a = base / "folder_a"
        dir_b = base / "folder_b"
        dir_c = base / "folder_c"
        dir_a.mkdir()
        dir_b.mkdir()
        dir_c.mkdir()

        # Group 1: 3 identical files (150 KB each)
        content_1 = b"Group 1 identical content " * 6000
        f1_a = dir_a / "file1_oldest.bin"
        f1_b = dir_b / "file1_newest.bin"
        f1_c = dir_c / "file1_short.bin"
        f1_a.write_bytes(content_1)
        f1_b.write_bytes(content_1)
        f1_c.write_bytes(content_1)

        # Set specific timestamps for retention policy testing
        t0 = time.time() - 1000
        t1 = time.time() - 500
        t2 = time.time()
        os.utime(f1_a, (t0, t0))
        os.utime(f1_c, (t1, t1))
        os.utime(f1_b, (t2, t2))

        # Group 2: 2 files with SAME size (100 KB) but DIFFERENT content
        # (Must be filtered out in Phase 2/3 and NOT reported as duplicates)
        f2_a = dir_a / "diff_a.bin"
        f2_b = dir_b / "diff_b.bin"
        f2_a.write_bytes(b"A" * 102400)
        f2_b.write_bytes(b"B" * 102400)

        # Unique file (different size)
        f_unique = dir_a / "unique.bin"
        f_unique.write_bytes(b"Unique content" * 100)

        yield {
            "base": base,
            "dir_a": dir_a,
            "dir_b": dir_b,
            "dir_c": dir_c,
            "g1_files": [f1_a, f1_b, f1_c],
            "g2_files": [f2_a, f2_b],
            "unique": f_unique,
        }


def test_scanner_engine_detects_true_duplicates(temp_test_environment):
    env = temp_test_environment
    engine = DuplicateScannerEngine(
        roots=[env["base"]],
        min_size_bytes=1024,
        workers=2
    )
    engine.run()

    # Only 1 true duplicate group should be found (Group 1)
    assert len(engine.duplicate_groups) == 1
    group = engine.duplicate_groups[0]
    assert len(group) == 3

    # Check file sizes in metadata
    for p in group:
        sz, mtime, digest = engine.file_meta[p]
        assert sz == len(env["g1_files"][0].read_bytes())
        assert len(digest) == 64  # Valid SHA-256 hex length


def test_retention_policy_oldest(temp_test_environment):
    env = temp_test_environment
    engine = DuplicateScannerEngine(roots=[env["base"]], min_size_bytes=1024)
    engine.run()

    group = engine.duplicate_groups[0]
    keeper, dups = DuplicateScannerEngine.partition_group(group, engine.file_meta, policy="oldest")

    # file1_oldest.bin should be the keeper
    assert keeper.name == "file1_oldest.bin"
    assert len(dups) == 2


def test_retention_policy_newest(temp_test_environment):
    env = temp_test_environment
    engine = DuplicateScannerEngine(roots=[env["base"]], min_size_bytes=1024)
    engine.run()

    group = engine.duplicate_groups[0]
    keeper, dups = DuplicateScannerEngine.partition_group(group, engine.file_meta, policy="newest")

    # file1_newest.bin should be the keeper
    assert keeper.name == "file1_newest.bin"
    assert len(dups) == 2


def test_retention_policy_shortest(temp_test_environment):
    env = temp_test_environment
    engine = DuplicateScannerEngine(roots=[env["base"]], min_size_bytes=1024)
    engine.run()

    group = engine.duplicate_groups[0]
    keeper, dups = DuplicateScannerEngine.partition_group(group, engine.file_meta, policy="shortest")
    assert keeper in group
    # Shortest path string length
    assert len(str(keeper)) <= min(len(str(d)) for d in dups)


def test_export_csv_and_json(temp_test_environment):
    env = temp_test_environment
    engine = DuplicateScannerEngine(roots=[env["base"]], min_size_bytes=1024)
    engine.run()

    csv_path = env["base"] / "report.csv"
    json_path = env["base"] / "report.json"

    export_csv_report(csv_path, engine.duplicate_groups, engine.file_meta, policy="oldest")
    export_json_report(json_path, engine.duplicate_groups, engine.file_meta, policy="oldest")

    assert csv_path.exists()
    assert json_path.exists()

    csv_content = csv_path.read_text(encoding="utf-8")
    assert "KEEP_ORIGINAL" in csv_content
    assert "DUPLICATE" in csv_content
    assert "SHA-256" in csv_content

    json_content = json_path.read_text(encoding="utf-8")
    assert "generator" in json_content
    assert "reclaimable_bytes" in json_content


def test_force_delete_file(temp_test_environment):
    env = temp_test_environment
    target = env["base"] / "delete_me.txt"
    target.write_text("temporary data")
    assert target.exists()

    ok, msg = force_delete_file(target)
    assert ok is True
    assert not target.exists()


def test_fmt_size():
    assert fmt_size(500) == "500.0 B"
    assert fmt_size(1024) == "1.0 KB"
    assert fmt_size(1024 * 1024) == "1.0 MB"
    assert fmt_size(1024 * 1024 * 1024) == "1.0 GB"

import time
import os
import pytest
from pathlib import Path
import librarymanager_core
from librarymanager_core import (
    get_backlog_items,
    invalidate_incoming_discovery_cache,
    discover_incoming_files_cached,
)


def test_two_refreshes_within_ttl_walk_tree_once(tmp_path, monkeypatch):
    inc = tmp_path / "Incoming"
    inc.mkdir()
    (inc / "Video 1.mp4").write_bytes(b"content 1")

    invalidate_incoming_discovery_cache()

    walk_calls = []
    real_walk = os.walk

    def tracked_walk(*args, **kwargs):
        walk_calls.append(args)
        return real_walk(*args, **kwargs)

    monkeypatch.setattr(os, "walk", tracked_walk)

    # First call: cache miss, walks tree
    res1 = discover_incoming_files_cached([str(inc)], ttl=3600.0)
    assert len(res1) == 1
    assert len(walk_calls) == 1

    # Second call within TTL: cache hit, no os.walk call
    res2 = discover_incoming_files_cached([str(inc)], ttl=3600.0)
    assert len(res2) == 1
    assert len(walk_calls) == 1


def test_explicit_force_refresh_walks_again(tmp_path, monkeypatch):
    inc = tmp_path / "Incoming"
    inc.mkdir()
    (inc / "Video 1.mp4").write_bytes(b"content 1")

    invalidate_incoming_discovery_cache()

    walk_calls = []
    real_walk = os.walk

    def tracked_walk(*args, **kwargs):
        walk_calls.append(args)
        return real_walk(*args, **kwargs)

    monkeypatch.setattr(os, "walk", tracked_walk)

    discover_incoming_files_cached([str(inc)], ttl=3600.0)
    assert len(walk_calls) == 1

    # Explicit force_refresh walks again
    discover_incoming_files_cached([str(inc)], force_refresh=True, ttl=3600.0)
    assert len(walk_calls) == 2


def test_config_root_change_invalidates_cache(tmp_path, monkeypatch):
    inc1 = tmp_path / "Incoming1"
    inc2 = tmp_path / "Incoming2"
    inc1.mkdir()
    inc2.mkdir()
    (inc1 / "Video 1.mp4").write_bytes(b"content 1")
    (inc2 / "Video 2.mp4").write_bytes(b"content 2")

    invalidate_incoming_discovery_cache()

    walk_calls = []
    real_walk = os.walk

    def tracked_walk(*args, **kwargs):
        walk_calls.append(args)
        return real_walk(*args, **kwargs)

    monkeypatch.setattr(os, "walk", tracked_walk)

    res1 = discover_incoming_files_cached([str(inc1)], ttl=3600.0)
    assert len(res1) == 1
    assert len(walk_calls) == 1

    # Calling with a different incoming root triggers fresh discovery
    res2 = discover_incoming_files_cached([str(inc2)], ttl=3600.0)
    assert len(res2) == 1
    assert len(walk_calls) == 2


def test_cache_expiry_walks_again(tmp_path, monkeypatch):
    inc = tmp_path / "Incoming"
    inc.mkdir()
    (inc / "Video 1.mp4").write_bytes(b"content 1")

    invalidate_incoming_discovery_cache()

    walk_calls = []
    real_walk = os.walk

    def tracked_walk(*args, **kwargs):
        walk_calls.append(args)
        return real_walk(*args, **kwargs)

    monkeypatch.setattr(os, "walk", tracked_walk)

    # First call with short TTL
    discover_incoming_files_cached([str(inc)], ttl=0.01)
    assert len(walk_calls) == 1

    time.sleep(0.03)

    # Cache expired, walks again
    discover_incoming_files_cached([str(inc)], ttl=0.01)
    assert len(walk_calls) == 2


def test_get_backlog_items_passes_force_refresh(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    inc = tmp_path / "Incoming"
    inc.mkdir()
    (inc / "Video 1.mp4").write_bytes(b"content 1")

    invalidate_incoming_discovery_cache()

    walk_calls = []
    real_walk = os.walk

    def tracked_walk(*args, **kwargs):
        walk_calls.append(args)
        return real_walk(*args, **kwargs)

    monkeypatch.setattr(os, "walk", tracked_walk)

    config = {"incomingFolders": [str(inc)]}
    get_backlog_items(db, None, config=config, force_refresh=False)
    assert len(walk_calls) == 1

    # Second metadata-only refresh
    get_backlog_items(db, None, config=config, force_refresh=False)
    assert len(walk_calls) == 1

    # Explicit recheck / force refresh
    get_backlog_items(db, None, config=config, force_refresh=True)
    assert len(walk_calls) == 2

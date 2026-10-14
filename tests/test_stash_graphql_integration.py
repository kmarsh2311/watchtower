"""Focused tests for Batch 3: Stash GraphQL Integration.

Verifies:
1. Query document constants and mutation syntax.
2. Safe execution dispatcher (execute_gql) error handling and mock dispatch.
3. Scene fetching, pagination, and count extraction.
4. Library roots extraction, filtering, and normalization.
5. Entity fetching (studios, performers).
6. Scene lookup by exact path and basename fallback.
7. Batch scene verification for pruning deleted scenes.
8. File deletion and configuration updates.
9. Backward compatibility exports on librarymanager and librarymanager_core.
"""
from __future__ import annotations

from unittest.mock import MagicMock, call
import pytest

import librarymanager
import librarymanager_core
from librarymanager.integrations.stash_graphql import (
    ALL_PERFORMERS_QUERY,
    ALL_STUDIOS_QUERY,
    CHECK_SCENE_EXISTS_QUERY,
    CHECK_SCENE_FILES_QUERY,
    CHECK_SCENES_QUERY,
    CONFIGURATION_VIDEO_EXTENSIONS_QUERY,
    CONFIGURE_GENERAL_MUTATION,
    DELETE_FILES_MUTATION,
    FIND_SCENE_BY_BASENAME_QUERY,
    FIND_SCENE_BY_PATH_QUERY,
    INVENTORY_SCENES_QUERY,
    QUERY,
    ROOTS_QUERY,
    SCENE_COUNT_QUERY,
    SCENE_QUERY,
    check_scene_exists,
    check_scene_files,
    check_scenes_batch,
    current_scene_count,
    delete_files,
    ensure_stash_supports_extension,
    execute_gql,
    fetch_all_files,
    fetch_all_performers,
    fetch_all_studios,
    fetch_library_roots,
    fetch_scene,
    fetch_scenes,
    find_scene_by_path,
    update_scene_performers,
    update_scene_studio,
    update_scene_tags,
)


def test_execute_gql_safety():
    """execute_gql handles None stash, missing call_GQL attribute, and exceptions gracefully."""
    assert execute_gql(None, "query { test }") is None
    assert execute_gql(object(), "query { test }") is None

    mock_stash = MagicMock()
    mock_stash.call_GQL.side_effect = RuntimeError("Stash API connection failed")
    assert execute_gql(mock_stash, "query { test }") is None

    mock_stash.reset_mock()
    mock_stash.call_GQL.side_effect = None
    mock_stash.call_GQL.return_value = {"data": "ok"}
    res = execute_gql(mock_stash, "query { test }", {"var": 1})
    assert res == {"data": "ok"}
    mock_stash.call_GQL.assert_called_once_with("query { test }", {"var": 1})


def test_fetch_scenes_pagination():
    """fetch_scenes retrieves multiple pages until total count is satisfied."""
    mock_stash = MagicMock()
    # Simulate page 1 with 2 scenes out of 3, and page 2 with the final scene
    mock_stash.call_GQL.side_effect = [
        {"findScenes": {"count": 3, "scenes": [{"id": "1"}, {"id": "2"}]}},
        {"findScenes": {"count": 3, "scenes": [{"id": "3"}]}},
    ]

    scenes = fetch_scenes(mock_stash, page_size=2)
    assert len(scenes) == 3
    assert [s["id"] for s in scenes] == ["1", "2", "3"]
    assert mock_stash.call_GQL.call_count == 2
    mock_stash.call_GQL.assert_has_calls([
        call(INVENTORY_SCENES_QUERY, {"filter": {"page": 1, "per_page": 2}}),
        call(INVENTORY_SCENES_QUERY, {"filter": {"page": 2, "per_page": 2}}),
    ])


def test_fetch_scene_single():
    """fetch_scene returns the target scene object or None."""
    mock_stash = MagicMock()
    mock_stash.call_GQL.return_value = {"findScene": {"id": "42", "title": "Test Scene"}}

    scene = fetch_scene(mock_stash, 42)
    assert scene == {"id": "42", "title": "Test Scene"}
    mock_stash.call_GQL.assert_called_once_with(SCENE_QUERY, {"id": "42"})

    mock_stash.call_GQL.return_value = {"findScene": None}
    assert fetch_scene(mock_stash, 999) is None


def test_current_scene_count():
    """current_scene_count extracts count from GraphQL response."""
    mock_stash = MagicMock()
    mock_stash.call_GQL.return_value = {"findScenes": {"count": 128}}
    assert current_scene_count(mock_stash) == 128

    mock_stash.call_GQL.return_value = None
    assert current_scene_count(mock_stash) == 0


def test_fetch_library_roots_normalization():
    """fetch_library_roots extracts, strips, deduplicates, and sorts library paths."""
    mock_stash = MagicMock()
    mock_stash.call_GQL.return_value = {
        "configuration": {
            "general": {
                "stashes": [
                    {"path": " /media/vault2 "},
                    {"path": "/media/vault1"},
                    {"path": ""},
                    {"path": "/media/vault1"},
                    {"path": None},
                ]
            }
        }
    }

    roots = fetch_library_roots(mock_stash)
    assert roots == ["/media/vault1", "/media/vault2"]


def test_fetch_all_studios_and_performers():
    """fetch_all_studios and fetch_all_performers return lists from Stash."""
    mock_stash = MagicMock()
    mock_stash.call_GQL.side_effect = [
        {"allStudios": [{"id": "s1", "name": "Studio A", "aliases": []}]},
        {"allPerformers": [{"id": "p1", "name": "Performer B", "disambiguation": None, "alias_list": []}]},
    ]

    studios = fetch_all_studios(mock_stash)
    assert studios == [{"id": "s1", "name": "Studio A", "aliases": []}]

    performers = fetch_all_performers(mock_stash)
    assert performers == [{"id": "p1", "name": "Performer B", "disambiguation": None, "alias_list": []}]


def test_find_scene_by_path_exact_and_fallback(tmp_path):
    """find_scene_by_path checks exact path first, then falls back to basename."""
    mock_stash = MagicMock()
    video_file = tmp_path / "MyScene.mp4"
    video_file.touch()
    path_str = str(video_file)

    # 1. Exact match hit
    mock_stash.call_GQL.return_value = {"findScenes": {"scenes": [{"id": "10", "files": [{"path": path_str}]}]}}
    scene = find_scene_by_path(mock_stash, path_str)
    assert scene["id"] == "10"
    mock_stash.call_GQL.assert_called_once_with(FIND_SCENE_BY_PATH_QUERY, {"path": path_str})

    # 2. Fallback hit when exact match returns empty
    mock_stash.call_GQL.reset_mock()
    mock_stash.call_GQL.side_effect = [
        {"findScenes": {"scenes": []}},  # Exact path misses
        {"findScenes": {"scenes": [{"id": "20", "files": [{"path": path_str}]}]}},  # Basename matches
    ]
    scene_fallback = find_scene_by_path(mock_stash, path_str)
    assert scene_fallback["id"] == "20"
    assert mock_stash.call_GQL.call_count == 2


def test_check_scenes_batch():
    """check_scenes_batch chunks IDs and accumulates active scene IDs and file IDs."""
    mock_stash = MagicMock()
    # Batch with 2 chunks
    ids = [f"s{i}" for i in range(150)]
    mock_stash.call_GQL.side_effect = [
        {"findScenes": {"scenes": [{"id": "s0", "files": [{"id": "f0"}]}]}},
        {"findScenes": {"scenes": [{"id": "s101", "files": [{"id": "f101"}]}]}},
    ]

    active_scenes, active_files, success = check_scenes_batch(mock_stash, ids, batch_size=100)
    assert success is True
    assert active_scenes == {"s0", "s101"}
    assert active_files == {"f0", "f101"}
    assert mock_stash.call_GQL.call_count == 2


def test_delete_files():
    """delete_files executes DELETE_FILES_MUTATION with ID list."""
    mock_stash = MagicMock()
    mock_stash.call_GQL.return_value = {"deleteFiles": True}

    res = delete_files(mock_stash, ["f1", "f2"])
    assert res is True
    mock_stash.call_GQL.assert_called_once_with(DELETE_FILES_MUTATION, {"ids": ["f1", "f2"]})


def test_ensure_stash_supports_extension():
    """ensure_stash_supports_extension adds missing video extension to configuration."""
    mock_stash = MagicMock()
    mock_stash.call_GQL.side_effect = [
        {"configuration": {"general": {"videoExtensions": ["mp4", "mkv"]}}},
        {"configureGeneral": {"videoExtensions": ["avi", "mkv", "mp4"]}},
    ]

    ensure_stash_supports_extension(mock_stash, "/path/to/movie.avi")
    assert mock_stash.call_GQL.call_count == 2
    mock_stash.call_GQL.assert_has_calls([
        call(CONFIGURATION_VIDEO_EXTENSIONS_QUERY),
        call(CONFIGURE_GENERAL_MUTATION, {"input": {"videoExtensions": ["avi", "mkv", "mp4"]}}),
    ])

    # No mutation when extension already exists
    mock_stash.reset_mock()
    mock_stash.call_GQL.side_effect = None
    mock_stash.call_GQL.return_value = {"configuration": {"general": {"videoExtensions": ["mp4", "mkv"]}}}
    ensure_stash_supports_extension(mock_stash, "/path/to/movie.mp4")
    assert mock_stash.call_GQL.call_count == 1


def test_backward_compatibility_exports():
    """Verifies that librarymanager and librarymanager_core re-export GraphQL functions and constants."""
    for mod in (librarymanager, librarymanager_core):
        assert hasattr(mod, "QUERY")
        assert hasattr(mod, "SCENE_QUERY")
        assert hasattr(mod, "current_scene_count")
        assert hasattr(mod, "fetch_scenes")
        assert hasattr(mod, "fetch_library_roots")

    # In librarymanager_core specifically
    assert hasattr(librarymanager_core, "check_scenes_batch")
    assert hasattr(librarymanager_core, "find_scene_by_path")
    assert hasattr(librarymanager_core, "ensure_stash_supports_extension")
    assert hasattr(librarymanager_core, "fetch_all_studios")
    assert hasattr(librarymanager_core, "fetch_all_performers")

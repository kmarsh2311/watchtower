import importlib
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import librarymanager_core
from librarymanager.database.connection import create_connection
from librarymanager.database.migrations import ensure_schema
from librarymanager.services.filing_rules import (
    NOISE_ALIASES,
    _compact_name_for_folder_match,
    _filter_subsumed_matches,
    _get_cached_destination_subdirectories,
    _is_excluded_destination_discovery_dir,
    _normalize_name_for_folder_match,
    _scan_destination_subdirectories,
    _strip_conservative_folder_descriptors,
    delete_filing_folder_mapping,
    discover_incoming_files_cached,
    get_configured_filing_destination_roots,
    get_filing_folder_mappings,
    invalidate_destination_dir_cache,
    invalidate_incoming_discovery_cache,
    match_performer_for_filing,
    match_studio_for_filing,
    refresh_destination_dir_cache,
    resolve_filing_destination_folder,
    resolve_filing_destinations,
    resolve_tag_filing_destinations,
    save_filing_folder_mapping,
    utc_now,
)
import librarymanager.services.filing_rules as filing_rules


class TestFilingRulesService(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        conn = create_connection(self.db_path)
        ensure_schema(conn, self.db_path)
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_direct_import_and_exports(self):
        self.assertTrue(callable(match_performer_for_filing))
        self.assertTrue(callable(match_studio_for_filing))
        self.assertTrue(callable(get_configured_filing_destination_roots))
        self.assertTrue(callable(get_filing_folder_mappings))
        self.assertTrue(callable(save_filing_folder_mapping))
        self.assertTrue(callable(delete_filing_folder_mapping))
        self.assertTrue(callable(invalidate_incoming_discovery_cache))
        self.assertTrue(callable(discover_incoming_files_cached))
        self.assertTrue(callable(invalidate_destination_dir_cache))
        self.assertTrue(callable(refresh_destination_dir_cache))
        self.assertTrue(callable(resolve_filing_destinations))
        self.assertTrue(callable(resolve_tag_filing_destinations))
        self.assertTrue(callable(resolve_filing_destination_folder))

        mod = importlib.import_module("librarymanager.services.filing_rules")
        for fn_name in mod.__all__:
            self.assertTrue(hasattr(mod, fn_name))

    def test_wrapper_delegation_markers(self):
        wrapper_funcs = [
            librarymanager_core.match_performer_for_filing,
            librarymanager_core.match_studio_for_filing,
            librarymanager_core.get_configured_filing_destination_roots,
            librarymanager_core.get_filing_folder_mappings,
            librarymanager_core.save_filing_folder_mapping,
            librarymanager_core.delete_filing_folder_mapping,
            librarymanager_core.invalidate_incoming_discovery_cache,
            librarymanager_core.discover_incoming_files_cached,
            librarymanager_core.invalidate_destination_dir_cache,
            librarymanager_core.refresh_destination_dir_cache,
            librarymanager_core._is_excluded_destination_discovery_dir,
            librarymanager_core._scan_destination_subdirectories,
            librarymanager_core._get_cached_destination_subdirectories,
            librarymanager_core.resolve_filing_destinations,
            librarymanager_core.resolve_tag_filing_destinations,
            librarymanager_core.resolve_filing_destination_folder,
        ]
        for fn in wrapper_funcs:
            self.assertTrue(
                getattr(fn, "_is_watchtower_wrapper", False),
                f"{fn.__name__} must be marked with _is_watchtower_wrapper = True",
            )

    def test_normalization_and_strip_descriptors(self):
        self.assertEqual(_normalize_name_for_folder_match("Foo - Bar_Baz.123"), "foo bar baz 123")
        self.assertEqual(_compact_name_for_folder_match("Foo - Bar_Baz.123"), "foobarbaz123")
        self.assertEqual(
            _strip_conservative_folder_descriptors("the cole bentley collection"),
            "cole bentley",
        )
        self.assertEqual(
            _strip_conservative_folder_descriptors("the kyle polaski (michal stranik) collection"),
            "kyle polaski",
        )

    def test_matching_parity(self):
        performers = [
            {"id": "1", "name": "Jane Doe", "aliases": ["JD", "Jane"]},
            {"id": "2", "name": "John Smith", "aliases": []},
        ]
        studios = [
            {"id": "10", "name": "Acme Productions", "aliases": ["Acme"], "parent_studio": None},
        ]
        scene_empty = {"id": "100", "title": "", "performers": [], "studio": None}

        # match_performer_for_filing parity
        p_direct = match_performer_for_filing(scene_empty, "Jane Doe In Paris.mp4", performers)
        p_wrapper = librarymanager_core.match_performer_for_filing(scene_empty, "Jane Doe In Paris.mp4", performers)
        self.assertEqual(p_direct, p_wrapper)
        self.assertTrue(p_direct["matched"])
        self.assertEqual(p_direct["entities"][0]["name"], "Jane Doe")

        # Noise aliases rejection
        noise_perf = [{"id": "99", "name": "The Performer", "aliases": ["the", "all"]}]
        p_noise = match_performer_for_filing(scene_empty, "All in the family.mp4", noise_perf)
        self.assertFalse(p_noise["matched"])
        self.assertEqual(
            p_noise,
            librarymanager_core.match_performer_for_filing(scene_empty, "All in the family.mp4", noise_perf),
        )

        # match_studio_for_filing parity
        s_direct = match_studio_for_filing(scene_empty, "Acme Productions Release 01.mp4", studios)
        s_wrapper = librarymanager_core.match_studio_for_filing(scene_empty, "Acme Productions Release 01.mp4", studios)
        self.assertEqual(s_direct, s_wrapper)
        self.assertTrue(s_direct["matched"])
        self.assertEqual(s_direct["entity"]["name"], "Acme Productions")

    def test_folder_mappings_crud_parity(self):
        dest_root = Path(self.tmp.name) / "Dest"
        dest_root.mkdir()
        target_dir = dest_root / "Custom_Folder"
        target_dir.mkdir()

        # Save mapping via service
        ok, msg = save_filing_folder_mapping(
            self.db_path,
            "performer",
            "101",
            "Custom Performer",
            str(target_dir),
            configured_roots=[str(dest_root)],
        )
        self.assertTrue(ok)

        # Retrieve via wrapper
        mappings_wrap = librarymanager_core.get_filing_folder_mappings(self.db_path)
        self.assertEqual(len(mappings_wrap), 1)
        self.assertEqual(mappings_wrap[0]["entity_id"], "101")
        self.assertEqual(mappings_wrap[0]["folder_path"], str(target_dir.resolve()))

        # Retrieve via service
        mappings_dir = get_filing_folder_mappings(self.db_path)
        self.assertEqual(mappings_wrap, mappings_dir)

        # Delete via wrapper
        mapping_id = mappings_wrap[0]["id"]
        del_ok = librarymanager_core.delete_filing_folder_mapping(self.db_path, mapping_id)
        self.assertTrue(del_ok)
        self.assertEqual(len(get_filing_folder_mappings(self.db_path)), 0)

    def test_destination_resolution_and_cache(self):
        dest_root = Path(self.tmp.name) / "DestRoot"
        dest_root.mkdir()
        sub1 = dest_root / "Studios" / "Acme"
        sub1.mkdir(parents=True)

        invalidate_destination_dir_cache(dest_root, self.db_path)

        # Direct resolution
        paths, status = resolve_filing_destinations(
            [str(dest_root)],
            "Acme",
            database_path=self.db_path,
        )
        self.assertEqual(status, "ok")
        self.assertEqual(paths, [sub1.resolve()])

        # Wrapper resolution should return same
        paths_w, status_w = librarymanager_core.resolve_filing_destinations(
            [str(dest_root)],
            "Acme",
            database_path=self.db_path,
        )
        self.assertEqual(paths_w, paths)
        self.assertEqual(status_w, status)

    def test_monkeypatched_scan_destination_subdirectories_respected(self):
        dest_root = Path(self.tmp.name) / "DestMonkey"
        dest_root.mkdir()

        mock_dirs = [(dest_root / "MockedFolder", "mockedfolder")]

        # Monkeypatch librarymanager_core._scan_destination_subdirectories
        with patch.object(
            librarymanager_core,
            "_scan_destination_subdirectories",
            return_value=mock_dirs,
        ) as mock_scan:
            invalidate_destination_dir_cache(dest_root)
            results = _get_cached_destination_subdirectories(dest_root)
            self.assertEqual(results, mock_dirs)
            mock_scan.assert_called_once()

    def test_excluded_destination_discovery_dir(self):
        self.assertTrue(_is_excluded_destination_discovery_dir(".git"))
        self.assertTrue(_is_excluded_destination_discovery_dir("@eaDir"))
        self.assertTrue(_is_excluded_destination_discovery_dir(".stfolder"))
        self.assertFalse(_is_excluded_destination_discovery_dir("Performers"))
        self.assertFalse(_is_excluded_destination_discovery_dir("Studios"))

    def test_persistent_destination_cache_expiry_and_age_calculation(self):
        vault = Path(self.tmp.name) / "VaultExpiry"
        vault.mkdir()
        sub = vault / "Studios" / "Studio1"
        sub.mkdir(parents=True)
        root_key = str(vault.resolve())

        # 1. Insert an expired entry (scanned 2 hours ago with TTL = 60s)
        two_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")
        stale_fake_sub = vault / "Studios" / "StaleOldFolder"
        conn = create_connection(self.db_path)
        conn.execute(
            "INSERT INTO filing_destination_cache_meta (root_path, max_depth, scanned_at, entry_count, generation) VALUES (?, ?, ?, ?, ?)",
            (root_key, 4, two_hours_ago, 1, 0),
        )
        conn.execute(
            "INSERT INTO filing_destination_dir_cache (root_path, dir_path, norm_name, depth) VALUES (?, ?, ?, ?)",
            (root_key, str(stale_fake_sub), "staleoldfolder", 2),
        )
        conn.commit()
        conn.close()

        # Invalidate in-memory cache so it must evaluate persistent SQLite cache
        invalidate_destination_dir_cache(vault)

        # Call with ttl=60s; entry is 2 hours old, so persistent cache must expire and disk be rescanned
        entries = _get_cached_destination_subdirectories(vault, max_depth=4, ttl=60.0, database_path=self.db_path)
        entry_paths = [str(p) for p, _ in entries]
        self.assertIn(str(sub.resolve()), entry_paths)
        self.assertNotIn(str(stale_fake_sub), entry_paths)

        # 2. Update metadata with an unexpired entry (scanned 5 seconds ago with TTL = 60s)
        five_sec_ago = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(timespec="seconds")
        cached_sub = vault / "Studios" / "CachedValidFolder"
        current_gen = filing_rules._CACHE_GENERATION
        conn = create_connection(self.db_path)
        conn.execute(
            "UPDATE filing_destination_cache_meta SET scanned_at=?, entry_count=1, generation=? WHERE root_path=?",
            (five_sec_ago, current_gen, root_key),
        )
        conn.execute("DELETE FROM filing_destination_dir_cache WHERE root_path=?", (root_key,))
        conn.execute(
            "INSERT INTO filing_destination_dir_cache (root_path, dir_path, norm_name, depth) VALUES (?, ?, ?, ?)",
            (root_key, str(cached_sub.resolve()), "cachedvalidfolder", 2),
        )
        conn.commit()
        conn.close()

        # Clear in-memory cache directly without incrementing generation
        filing_rules._DESTINATION_DIR_CACHE.clear()

        # With ttl=60s, the unexpired entry must be recognized as fresh and returned directly from DB
        entries_valid = _get_cached_destination_subdirectories(vault, max_depth=4, ttl=60.0, database_path=self.db_path)
        valid_paths = [str(p) for p, _ in entries_valid]
        self.assertIn(str(cached_sub.resolve()), valid_paths)

    def test_destination_path_resolution_exception_fallback(self):
        # Simulate an unexpected OSError during path resolution
        bad_path = MagicMock()
        bad_path.expanduser.side_effect = OSError("Simulated filesystem I/O error")

        mock_fallback = [(Path("/mock/fallback"), "mockfallback")]
        with patch.object(
            librarymanager_core,
            "_scan_destination_subdirectories",
            return_value=mock_fallback,
        ) as mock_scan:
            # Must execute fallback scan without raising TypeError or getattr error
            results = _get_cached_destination_subdirectories(bad_path)
            self.assertEqual(results, mock_fallback)
            mock_scan.assert_called_once_with(bad_path, 4)

    def test_cache_generation_compatibility_and_synchronization(self):
        initial_gen = librarymanager_core._CACHE_GENERATION
        self.assertEqual(initial_gen, filing_rules._CACHE_GENERATION)

        # Invalidation via service increments generation in both modules
        invalidate_destination_dir_cache()
        self.assertEqual(filing_rules._CACHE_GENERATION, initial_gen + 1)
        self.assertEqual(librarymanager_core._CACHE_GENERATION, initial_gen + 1)

        # Invalidation via librarymanager_core wrapper increments generation in both modules
        librarymanager_core.invalidate_destination_dir_cache()
        self.assertEqual(filing_rules._CACHE_GENERATION, initial_gen + 2)
        self.assertEqual(librarymanager_core._CACHE_GENERATION, initial_gen + 2)

    def test_utc_now_precision(self):
        ts = utc_now()
        # Verify ISO 8601 format
        dt = datetime.fromisoformat(ts)
        self.assertIsNotNone(dt.tzinfo)
        # Verify second precision (no fractional seconds/microseconds)
        self.assertEqual(len(ts.split(".")), 1, f"Timestamp should not have fractional seconds: {ts}")
        # Verify timezone offset present
        self.assertTrue("+" in ts or "Z" in ts or "-" in ts)

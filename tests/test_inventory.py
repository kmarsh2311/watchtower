import sqlite3
import tempfile
import time
import unittest
import os
from pathlib import Path

from librarymanager_core import (build_merge_preview, build_resolution_plan, inventory,
                                 opensubtitles_hash, preview_safe_filenames, preview_scene_filename,
                                 apply_manual_filename, apply_scene_filename, claim_due_rename, enqueue_rename,
                                 finish_queued_rename, reconcile_missing_files, refresh_scene_inventory,
                                 record_filesystem_event, filesystem_monitor_summary,
                                 reconcile_filesystem_events, record_activity, recent_activity)
from librarymanager_core import preview_manual_filename
from librarymanager_core import consume_expected_move, expect_filesystem_move, resolve_filesystem_event
from librarymanager_core import scene_naming_signature
from librarymanager_core import _proposed_stem, incoming_summary
from librarymanager import automatic_scene_allowed, incoming_folder_status, incoming_folders_status, get_configured_incoming_folders
from librarymanager_monitor import CompletedDownloadWorker, tracked_move


class InventoryTests(unittest.TestCase):
    def test_completed_download_waits_then_runs_one_targeted_scan(self):
        class FakeStash:
            def __init__(self, path):
                self.path = path
                self.scans = []

            def metadata_scan(self, paths, flags=None):
                self.scans.append(paths)
                self.flags = flags
                return "91"

            def wait_for_job(self, job_id, timeout):
                return True

            def call_GQL(self, _query, variables):
                self.asserted_path = variables["path"]
                return {"findScenes": {"scenes": [{
                    "id": "12", "title": "", "studio": None, "performers": [], "tags": [],
                    "galleries": [], "stash_ids": [], "groups": [], "urls": [],
                    "files": [{"id": "33", "path": self.path, "basename": Path(self.path).name,
                               "size": Path(self.path).stat().st_size, "duration": 1, "fingerprints": []}],
                }]}}

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            database = root / "inventory.sqlite3"
            incoming = root / "Incoming"
            incoming.mkdir()
            worker = CompletedDownloadWorker(database, FakeStash(""), incoming, True, 60, False)
            video = incoming / "finished.mp4"
            video.write_bytes(b"video")
            worker.stash.path = str(video.resolve())
            self.assertTrue(worker.submit(video))
            candidate = worker.candidates[str(video.resolve())]
            worker.evaluate_once(candidate["stable_since"] + 59)
            self.assertEqual(worker.stash.scans, [])
            worker.evaluate_once(candidate["stable_since"] + 60)
            self.assertEqual(worker.stash.scans, [[str(video.resolve())]])
            self.assertTrue(worker.stash.flags["scanGeneratePreviews"])
            self.assertTrue(worker.stash.flags["scanGenerateSprites"])
            self.assertEqual(incoming_summary(database)["imported"], 1)

    def test_completed_download_ignores_temporary_and_preexisting_files(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            database = root / "inventory.sqlite3"
            incoming = root / "Incoming"
            incoming.mkdir()
            existing = incoming / "existing.mp4"
            existing.write_bytes(b"old")
            old_time = time.time() - 600
            os.utime(existing, (old_time, old_time))
            temporary = incoming / "new.mp4.part"
            temporary.write_bytes(b"partial")
            worker = CompletedDownloadWorker(database, object(), incoming, True, 300, False)
            worker._fallback_check()
            self.assertEqual(worker.candidates, {})
            self.assertFalse(worker.submit(temporary))

    def test_restart_recovers_recent_direct_final_video_but_not_old_files(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            incoming = root / "Incoming"
            incoming.mkdir()
            old = incoming / "old.mp4"
            old.write_bytes(b"old")
            old_time = time.time() - 600
            os.utime(old, (old_time, old_time))
            recent = incoming / "direct-final.m4v"
            recent.write_bytes(b"new")
            worker = CompletedDownloadWorker(root / "inventory.sqlite3", object(), incoming, True, 300, False)
            self.assertIn(str(recent.resolve()), worker.candidates)
            self.assertNotIn(str(old.resolve()), worker.candidates)

    def test_new_download_subfolder_discovers_all_nested_videos(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            incoming = root / "Incoming"
            nested = incoming / "Torrent" / "Disc 1"
            nested.mkdir(parents=True)
            (nested / "one.mp4").write_bytes(b"one")
            (nested / "two.avi").write_bytes(b"two")
            (nested / "unfinished.mp4.part").write_bytes(b"partial")
            worker = CompletedDownloadWorker(root / "inventory.sqlite3", object(), incoming, True, 300, False)
            worker.candidates.clear()
            self.assertEqual(worker.submit_tree(incoming / "Torrent"), 2)

    def test_file_change_restarts_settle_timer_after_pause(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            incoming = root / "Incoming"
            incoming.mkdir()
            video = incoming / "paused.mp4"
            video.write_bytes(b"first")
            worker = CompletedDownloadWorker(root / "inventory.sqlite3", object(), incoming, True, 300, False)
            path = str(video.resolve())
            previous = worker.candidates[path]["stable_since"]
            video.write_bytes(b"resumed download")
            worker.evaluate_once(previous + 120)
            self.assertEqual(worker.candidates[path]["stable_since"], previous + 120)

    def test_pending_download_move_preserves_wait_state_outside_incoming_folder(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            incoming = root / "Incoming"
            library = root / "Library"
            incoming.mkdir()
            library.mkdir()
            source = incoming / "video.mp4"
            source.write_bytes(b"video")
            worker = CompletedDownloadWorker(root / "inventory.sqlite3", object(), incoming, True, 300, False)
            old_path = str(source.resolve())
            stable_since = worker.candidates[old_path]["stable_since"]
            destination = library / "video.mp4"
            source.rename(destination)
            self.assertTrue(worker.relocate(source, destination))
            new_path = str(destination.resolve())
            self.assertNotIn(old_path, worker.candidates)
            self.assertEqual(worker.candidates[new_path]["stable_since"], stable_since)
            self.assertEqual(worker._resolve_relocation(old_path), new_path)

    def test_move_during_stash_scan_follows_destination_without_review_failure(self):
        class MovingStash:
            def __init__(self):
                self.scans = []
                self.worker = None
                self.source = None
                self.destination = None

            def metadata_scan(self, paths, flags=None):
                self.scans.append(paths)
                return str(len(self.scans))

            def wait_for_job(self, _job_id, timeout):
                if len(self.scans) == 1:
                    Path(self.source).rename(self.destination)
                    self.worker.relocate(self.source, self.destination)
                return True

            def call_GQL(self, _query, variables):
                path = variables["path"]
                return {"findScenes": {"scenes": [{
                    "id": "77", "title": "", "studio": None, "performers": [], "tags": [],
                    "galleries": [], "stash_ids": [], "groups": [], "urls": [],
                    "files": [{"id": "88", "path": path, "basename": Path(path).name,
                               "size": Path(path).stat().st_size, "duration": 1, "fingerprints": []}],
                }]}}

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            incoming = root / "Incoming"
            library = root / "Library"
            incoming.mkdir()
            library.mkdir()
            source = incoming / "moving.mp4"
            destination = library / "moving.mp4"
            source.write_bytes(b"video")
            stash = MovingStash()
            worker = CompletedDownloadWorker(root / "inventory.sqlite3", stash, incoming, True, 60, False)
            stash.worker, stash.source, stash.destination = worker, str(source.resolve()), str(destination.resolve())
            candidate = worker.candidates.pop(str(source.resolve()))
            self.assertTrue(worker._scan(str(source.resolve()), candidate))
            self.assertEqual(stash.scans, [[str(source.resolve())], [str(destination.resolve())]])
            self.assertEqual(incoming_summary(root / "inventory.sqlite3")["imported"], 1)

    def test_incoming_folder_must_be_inside_a_stash_library(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            library = root / "Library"
            incoming = library / "Incoming"
            outside = root / "Downloads"
            incoming.mkdir(parents=True)
            outside.mkdir()
            self.assertTrue(incoming_folder_status({"automaticIncomingScan": True, "incomingFolder": str(incoming)}, [str(library)])["valid"])
            self.assertFalse(incoming_folder_status({"automaticIncomingScan": True, "incomingFolder": str(outside)}, [str(library)])["valid"])

    def test_multi_incoming_folders_validation_and_cap(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            lib1 = root / "Vault1"
            lib2 = root / "Vault2"
            inc1 = lib1 / "Torrents"
            inc2 = lib2 / "JDownloader"
            inc3 = lib1 / "AirDrop"
            outside = root / "Outside"
            for p in [inc1, inc2, inc3, outside]:
                p.mkdir(parents=True)

            roots = [str(lib1), str(lib2)]

            # Test legacy fallback
            self.assertEqual(get_configured_incoming_folders({"incomingFolder": str(inc1)}), [str(inc1)])

            # Test capping at 5
            six_folders = [str(inc1), str(inc2), str(inc3), str(lib1 / "f4"), str(lib1 / "f5"), str(lib1 / "f6")]
            capped = get_configured_incoming_folders({"incomingFolders": six_folders})
            self.assertEqual(len(capped), 5)

            # Test multi-folder status
            status = incoming_folders_status({"automaticIncomingScan": True, "incomingFolders": [str(inc1), str(inc2), str(outside)]}, roots)
            self.assertEqual(status["total_count"], 3)
            self.assertEqual(status["valid_count"], 2)
            self.assertFalse(status["all_valid"])
            self.assertTrue(status["folders"][0]["valid"])
            self.assertTrue(status["folders"][1]["valid"])
            self.assertFalse(status["folders"][2]["valid"])

    def test_completed_download_worker_multi_folder_ingest(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            database = root / "inventory.sqlite3"
            incA = root / "IncomingA"
            incB = root / "IncomingB"
            incA.mkdir()
            incB.mkdir()
            worker = CompletedDownloadWorker(database, object(), None, True, 300, False, incoming_folders=[str(incA), str(incB)])
            videoA = incA / "videoA.mp4"
            videoB = incB / "videoB.mkv"
            videoA.write_bytes(b"contentA")
            videoB.write_bytes(b"contentB")
            self.assertTrue(worker.submit(videoA))
            self.assertTrue(worker.submit(videoB))
            self.assertIn(str(videoA.resolve()), worker.candidates)
            self.assertIn(str(videoB.resolve()), worker.candidates)

    def test_filename_style_choices_control_order_and_separators(self):
        options = {
            "filenameOrder": "performers,title,studio",
            "filenameSectionSeparator": "space",
            "filenamePerformerSeparator": "space",
        }
        self.assertEqual(
            _proposed_stem("Example Scene", "Example Studio", ["Alex Smith", "Jamie Jones"], options),
            "Alex Smith Jamie Jones Example Scene Example Studio",
        )

    def test_proposed_stem_deduplicates_performer_only_titles(self):
        options = {
            "filenameOrder": "title,studio,performers",
            "filenameSectionSeparator": "dash",
            "filenamePerformerSeparator": "comma",
        }
        # Title only contains performers without punctuation
        self.assertEqual(
            _proposed_stem("Performer One Performer Two", None, ["Performer One", "Performer Two"], options),
            "Performer One, Performer Two",
        )
        # Title contains performers with "and"
        self.assertEqual(
            _proposed_stem("Performer One and Performer Two", None, ["Performer One", "Performer Two"], options),
            "Performer One, Performer Two",
        )
        # Title contains performers with "&" and studio
        self.assertEqual(
            _proposed_stem("Performer One & Performer Two", "Studio Alpha", ["Performer One", "Performer Two"], options),
            "Studio Alpha - Performer One, Performer Two",
        )
        # Title has real name + performers
        self.assertEqual(
            _proposed_stem("Morning Visit - Performer One & Performer Two", "Studio Alpha", ["Performer One", "Performer Two"], options),
            "Morning Visit - Studio Alpha - Performer One, Performer Two",
        )

    def test_proposed_stem_granular_rules(self):
        # 1. includeStudio=False
        opts_no_studio = {'includeStudio': False}
        self.assertEqual(
            _proposed_stem('Morning Coffee', 'Big Studio', ['Jane Doe'], opts_no_studio),
            'Morning Coffee - Jane Doe'
        )

        # 2. includePerformers=False
        opts_no_perfs = {'includePerformers': False}
        self.assertEqual(
            _proposed_stem('Morning Coffee', 'Big Studio', ['Jane Doe'], opts_no_perfs),
            'Morning Coffee - Big Studio'
        )

        # 3. maxPerformersInFilename limit
        opts_max_perfs = {'maxPerformersInFilename': 2}
        self.assertEqual(
            _proposed_stem('Big Scene', 'Studio', ['Performer 1', 'Performer 2', 'Performer 3', 'Performer 4'], opts_max_perfs),
            'Big Scene - Studio - Performer 1, Performer 2'
        )

        # 4. cleanPerformerOnlyTitles=False with stripPerformersFromTitle=False (preserves performer-only title completely)
        opts_no_dedup = {'cleanPerformerOnlyTitles': False, 'stripPerformersFromTitle': False}
        self.assertEqual(
            _proposed_stem('Performer One and Performer Two', None, ['Performer One', 'Performer Two'], opts_no_dedup),
            'Performer One and Performer Two - Performer One, Performer Two'
        )

        # 4b. Real title with performer name: strips performer from title when stripPerformersFromTitle=True
        opts_strip_perf = {'stripPerformersFromTitle': True}
        self.assertEqual(
            _proposed_stem('Performer One In The Summer', None, ['Performer One'], opts_strip_perf),
            'The Summer - Performer One'
        )

        # 5. stripStudioFromTitle=False (keeps embedded studio in title)
        opts_keep_studio = {'stripStudioFromTitle': False}
        self.assertEqual(
            _proposed_stem('Studio Name Episode 1', 'Studio Name', ['Performer A'], opts_keep_studio),
            'Studio Name Episode 1 - Studio Name - Performer A'
        )

        # 8. Partially tagged performers consuming attached conjunction (e.g. Prefix PerformerB & PerformerA)
        self.assertEqual(
            _proposed_stem('Prefix PerformerB & PerformerA Episode 3', 'Example Studio', ['PerformerA'], {}),
            'Prefix PerformerB Episode 3 - Example Studio - PerformerA'
        )

        # 7. Leftover conjunction with hyphen (e.g. Prefix PerformerOne & PerformerTwo - Scene Title FHD)
        self.assertEqual(
            _proposed_stem('Prefix PerformerOne & PerformerTwo - Scene Title FHD', 'Example Studio', ['PerformerTwo', 'PerformerOne'], {}),
            'Prefix - Scene Title FHD - Example Studio - PerformerTwo, PerformerOne'
        )

        # 6. stripPerformersFromTitle=False (keeps embedded performer in title)
        opts_keep_perf = {'stripPerformersFromTitle': False}
        self.assertEqual(
            _proposed_stem('Performer A at Beach', 'Studio Name', ['Performer A'], opts_keep_perf),
            'Performer A at Beach - Studio Name - Performer A'
        )

    def test_preview_safe_filenames_master_title_source(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            video = root / 'Original_Camera_Take.mp4'
            video.write_bytes(b'x' * 1000)
            database = root / 'inventory.sqlite3'
            
            # Scene has Stash title 'Scraped Title', Studio 'My Studio', and Performer 'Jane Doe'
            scene = {
                'id': '10',
                'title': 'Scraped Title',
                'studio': {'name': 'My Studio'},
                'performers': [{'name': 'Jane Doe'}],
                'files': [{'id': '20', 'path': str(video), 'size': 1000, 'fingerprints': []}]
            }
            inventory(database, [scene])

            # 1. Default (stash_title): uses 'Scraped Title'
            _, report_default = preview_safe_filenames(database, {'masterTitleSource': 'stash_title'})
            self.assertTrue(report_default[0]['proposed_path'].endswith('Scraped Title - My Studio - Jane Doe.mp4'))

            # 2. filename: uses 'Original_Camera_Take'
            _, report_filename = preview_safe_filenames(database, {'masterTitleSource': 'filename'})
            self.assertTrue(report_filename[0]['proposed_path'].endswith('Original_Camera_Take - My Studio - Jane Doe.mp4'))

    def test_expected_plugin_move_is_consumed_once(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Path(temporary_directory) / "inventory.sqlite3"
            expect_filesystem_move(database, "/old/video.mp4", "/new/video.mp4")
            self.assertTrue(consume_expected_move(database, "/old/video.mp4", "/new/video.mp4"))
            self.assertFalse(consume_expected_move(database, "/old/video.mp4", "/new/video.mp4"))

    def test_external_move_requires_exact_inventoried_source_and_identity(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source.mp4"
            source.write_bytes(b"x" * 200000)
            expected_hash = opensubtitles_hash(source)
            database = root / "inventory.sqlite3"
            scene = {"id": "10", "title": "", "studio": None, "performers": [],
                     "files": [{"id": "20", "path": str(source), "size": source.stat().st_size,
                                "fingerprints": [{"type": "oshash", "value": expected_hash}]}]}
            inventory(database, [scene])
            destination = root / "destination.mp4"
            source.rename(destination)
            row, reason = tracked_move(database, str(source), str(destination))
            self.assertEqual(row["file_id"], "20")
            self.assertIn("oshash", reason)
            other, other_reason = tracked_move(database, str(root / "unknown.mp4"), str(destination))
            self.assertIsNone(other)
            self.assertIn("not in the inventory", other_reason)

    def test_activity_log_keeps_paths_and_metadata(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Path(temporary_directory) / "inventory.sqlite3"
            record_activity(database, "rename", "automatic rename", "renamed", scene_id="10", file_id="20",
                            old_path="/old/video.mp4", new_path="/new/video.mp4",
                            detail="Stash confirmed", metadata={"trigger": "Scene.Update.Post"})
            rows = recent_activity(database)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["scene_id"], "10")
            self.assertEqual(rows[0]["old_path"], "/old/video.mp4")
            self.assertEqual(rows[0]["metadata"]["trigger"], "Scene.Update.Post")

    def test_automatic_scope_is_locked_to_configured_test_scene(self):
        self.assertTrue(automatic_scene_allowed({"testSceneId": "6364"}, "6364"))
        self.assertFalse(automatic_scene_allowed({"testSceneId": "6364"}, "9999"))
        self.assertTrue(automatic_scene_allowed({"testSceneId": ""}, "9999"))

    def test_rename_queue_coalesces_repeated_scene_hooks(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Path(temporary_directory) / "inventory.sqlite3"
            self.assertTrue(enqueue_rename(database, "10", 100.0, debounce_seconds=1.0))
            self.assertFalse(enqueue_rename(database, "10", 100.5, debounce_seconds=1.0))
            scene_id, next_at, pending = claim_due_rename(database, 101.0)
            self.assertIsNone(scene_id)
            self.assertEqual((next_at, pending), (101.5, 1))
            scene_id, _, _ = claim_due_rename(database, 101.5)
            self.assertEqual(scene_id, "10")
            finish_queued_rename(database, scene_id, "skipped", "test")

    def test_filesystem_events_are_read_only_and_coalesced(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            database = root / "inventory.sqlite3"
            video = root / "video.mp4"
            record_filesystem_event(database, "created", str(video))
            record_filesystem_event(database, "created", str(video))
            summary = filesystem_monitor_summary(database)
            self.assertEqual(summary["pending_events"], 1)
            connection = sqlite3.connect(database)
            count = connection.execute("SELECT event_count FROM filesystem_events").fetchone()[0]
            connection.close()
            self.assertEqual(count, 2)
            self.assertFalse(video.exists())

            reconcile_filesystem_events(database)
            # Analysis must not silently acknowledge an unresolved event. The
            # user chooses a resolution explicitly in the Watchtower UI.
            self.assertEqual(filesystem_monitor_summary(database)["pending_events"], 1)

    def test_successfully_handled_move_clears_attention_count(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Path(temporary_directory) / "inventory.sqlite3"
            record_filesystem_event(database, "moved", "/old/video.mp4", "/new/video.mp4")
            self.assertEqual(filesystem_monitor_summary(database)["pending_events"], 1)
            resolve_filesystem_event(database, "moved", "/old/video.mp4", "/new/video.mp4")
            self.assertEqual(filesystem_monitor_summary(database)["pending_events"], 0)

    def test_watched_move_is_verified_without_updating_stash_inventory(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            original = root / "old.mp4"
            original.write_bytes(bytes(range(256)) * 600)
            database = root / "inventory.sqlite3"
            file_hash = opensubtitles_hash(original)
            scene = {"id": "10", "files": [{"id": "20", "path": str(original),
                     "size": original.stat().st_size,
                     "fingerprints": [{"type": "oshash", "value": file_hash}]}]}
            inventory(database, [scene])
            moved = root / "new.mp4"
            original.rename(moved)
            record_filesystem_event(database, "moved", str(original), str(moved))
            summary, proposals = reconcile_filesystem_events(database)
            self.assertEqual(summary["verified"], 1)
            self.assertEqual(proposals[0]["recommendation"], "targeted_stash_reconciliation")
            connection = sqlite3.connect(database)
            stored_path = connection.execute("SELECT path FROM files WHERE file_id='20'").fetchone()[0]
            connection.close()
            self.assertEqual(stored_path, str(original))

    def test_records_present_and_missing_files_and_path_changes(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            existing = root / "video.mp4"
            existing.write_bytes(b"video")
            database = root / "inventory.sqlite3"
            scene = {"id": "10", "title": "Title", "studio": {"name": "Studio"},
                     "performers": [{"name": "Person"}], "files": [
                         {"id": "20", "path": str(existing), "basename": existing.name,
                          "size": 5, "duration": 1.0, "fingerprints": [{"type": "md5", "value": "abc"}]}
                     ]}
            first = inventory(database, [scene])
            self.assertEqual((first["present"], first["missing"]), (1, 0))
            self.assertEqual((first["scenes"], first["files"]), (1, 1))

            moved = root / "moved.mp4"
            scene["files"][0]["path"] = str(moved)
            scene["files"][0]["basename"] = moved.name
            second = inventory(database, [scene])
            self.assertEqual((second["missing"], second["changed_paths"]), (1, 1))

            connection = sqlite3.connect(database)
            events = [row[0] for row in connection.execute("SELECT event_type FROM inventory_events ORDER BY id")]
            connection.close()
            self.assertEqual(events, ["stash_path_changed", "file_missing"])

    def test_reconciliation_verifies_same_folder_rename_by_oshash(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            original = root / "old.mp4"
            original.write_bytes(bytes(range(256)) * 600)
            expected_hash = opensubtitles_hash(original)
            database = root / "inventory.sqlite3"
            scene = {"id": "10", "files": [{"id": "20", "path": str(original),
                     "basename": original.name, "size": original.stat().st_size,
                     "fingerprints": [{"type": "oshash", "value": expected_hash}]}]}
            inventory(database, [scene])
            renamed = root / "new.mp4"
            original.rename(renamed)
            inventory(database, [scene])

            summary, report = reconcile_missing_files(database)
            self.assertEqual(summary["matched"], 1)
            self.assertEqual(report[0]["candidate_path"], str(renamed))
            self.assertEqual(report[0]["confidence"], "verified")

    def test_reconciliation_reports_candidate_tracked_by_another_scene_as_conflict(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            missing = root / "old.mp4"
            existing = root / "new.mp4"
            existing.write_bytes(bytes(range(256)) * 600)
            file_hash = opensubtitles_hash(existing)
            database = root / "inventory.sqlite3"
            scenes = [
                {"id": "10", "files": [{"id": "20", "path": str(missing), "size": existing.stat().st_size,
                    "fingerprints": [{"type": "oshash", "value": file_hash}]}]},
                {"id": "11", "files": [{"id": "21", "path": str(existing), "size": existing.stat().st_size,
                    "fingerprints": [{"type": "oshash", "value": file_hash}]}]},
            ]
            inventory(database, scenes)
            summary, report = reconcile_missing_files(database)
            self.assertEqual(summary["ambiguous"], 1)
            self.assertEqual(report[0]["confidence"], "conflict")
            self.assertIn("file 21 on scene 11", report[0]["reason"])

            plan = build_resolution_plan(database)
            self.assertEqual(plan["safe_redundant"], 1)
            self.assertEqual(plan["items"][0]["recommendation"], "stale_record_redundant")

    def test_resolution_plan_requires_merge_for_stale_only_metadata(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            missing = root / "old.mp4"
            existing = root / "new.mp4"
            existing.write_bytes(bytes(range(256)) * 600)
            file_hash = opensubtitles_hash(existing)
            common_file = {"size": existing.stat().st_size,
                           "fingerprints": [{"type": "oshash", "value": file_hash}]}
            scenes = [
                {"id": "10", "tags": [{"id": "99", "name": "Stale only"}],
                 "files": [{"id": "20", "path": str(missing), **common_file}]},
                {"id": "11", "tags": [], "files": [{"id": "21", "path": str(existing), **common_file}]},
            ]
            database = root / "inventory.sqlite3"
            inventory(database, scenes)
            reconcile_missing_files(database)
            plan = build_resolution_plan(database)
            self.assertEqual(plan["merge_required"], 1)
            self.assertEqual(plan["items"][0]["metadata_differences"], ["tags"])
            preview = build_merge_preview(database)
            self.assertTrue(preview["previews"][0]["ready_to_apply"])
            self.assertEqual(preview["previews"][0]["changes"][0]["field"], "tags")
            self.assertFalse(preview["action_performed"])

    def test_merge_preview_never_overwrites_different_scalar_values(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            missing = root / "old.mp4"
            existing = root / "new.mp4"
            existing.write_bytes(bytes(range(256)) * 600)
            file_hash = opensubtitles_hash(existing)
            common_file = {"size": existing.stat().st_size,
                           "fingerprints": [{"type": "oshash", "value": file_hash}]}
            scenes = [
                {"id": "10", "date": "2020-01-01", "files": [{"id": "20", "path": str(missing), **common_file}]},
                {"id": "11", "date": "2021-01-01", "files": [{"id": "21", "path": str(existing), **common_file}]},
            ]
            database = root / "inventory.sqlite3"
            inventory(database, scenes)
            reconcile_missing_files(database)
            preview = build_merge_preview(database)
            self.assertFalse(preview["previews"][0]["ready_to_apply"])
            self.assertEqual(preview["previews"][0]["conflicts"][0]["field"], "date")

    def test_filename_preview_uses_title_and_never_renames(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            video = root / "original.mp4"
            video.write_bytes(b"video")
            database = root / "inventory.sqlite3"
            scene = {"id": "10", "title": "Scene Title", "studio": {"name": "Studio"},
                     "performers": [{"id": "1", "name": "Person"}],
                     "files": [{"id": "20", "path": str(video), "size": 5}]}
            inventory(database, [scene])
            summary, report = preview_safe_filenames(database)
            self.assertEqual(summary["proposed"], 1)
            self.assertTrue(report[0]["proposed_path"].endswith("Scene Title - Studio - Person.mp4"))
            self.assertTrue(video.exists())
            self.assertFalse(Path(report[0]["proposed_path"]).exists())

    def test_filename_preview_extracts_known_suffix_for_blank_title(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            video = root / "Original - {Studio} - (Person).mp4"
            video.write_bytes(b"video")
            database = root / "inventory.sqlite3"
            scene = {"id": "10", "title": "", "studio": {"name": "Studio"},
                     "performers": [{"id": "1", "name": "Person"}],
                     "files": [{"id": "20", "path": str(video), "size": 5}]}
            inventory(database, [scene])
            summary, report = preview_safe_filenames(database)
            self.assertEqual(summary["proposed"], 1)
            self.assertEqual(report[0]["base_stem"], "Original")
            self.assertTrue(report[0]["proposed_path"].endswith("Original - Studio - Person.mp4"))

    def test_filename_preview_replaces_compact_bracketed_studio_with_display_name(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            video = root / "[examplestudio] Scene.mp4"
            video.write_bytes(b"video")
            database = root / "inventory.sqlite3"
            scene = {"id": "10", "title": "", "studio": {"name": "Example Studio"},
                     "performers": [], "files": [{"id": "20", "path": str(video), "size": 5}]}
            inventory(database, [scene])
            _, first = preview_safe_filenames(database)
            self.assertEqual(first[0]["base_stem"], "Scene")
            self.assertTrue(first[0]["proposed_path"].endswith("Scene - Example Studio.mp4"))
            # Previously persisted filename bases are cleaned as well.
            with sqlite3.connect(database) as connection:
                connection.execute("UPDATE filename_state SET base_stem='[examplestudio] Scene'")
            _, second = preview_safe_filenames(database)
            self.assertEqual(second[0]["base_stem"], "Scene")

    def test_manual_filename_correction_is_previewed_and_moves_companion(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            video = root / "Wrong.mp4"
            image = root / "Wrong.jpg"
            video.write_bytes(b"video")
            image.write_bytes(b"image")
            database = root / "inventory.sqlite3"
            inventory(database, [{"id": "10", "title": "", "studio": None, "performers": [],
                                  "files": [{"id": "20", "path": str(video), "size": 5}]}])
            preview = preview_manual_filename(database, "10", "Correct.mp4")
            self.assertEqual(preview["status"], "ready")
            self.assertTrue(video.exists())

            def fake_move(_file_id, folder, basename):
                video.rename(Path(folder) / basename)
                return True

            result = apply_manual_filename(database, "10", "Correct.mp4", fake_move)
            self.assertEqual(result["status"], "renamed")
            self.assertTrue((root / "Correct.mp4").exists())
            self.assertTrue((root / "Correct.jpg").exists())

    def test_manual_correction_remembers_and_removes_later_unassigned_performer(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            video = root / "Original.mp4"
            video.write_bytes(b"video")
            database = root / "inventory.sqlite3"
            scene = {"id": "10", "title": "", "studio": None,
                     "performers": [{"id": "1", "name": "Alex Morgan"}],
                     "files": [{"id": "20", "path": str(video), "size": 5}]}
            inventory(database, [scene])

            def fake_move(_file_id, folder, basename):
                nonlocal video
                destination = Path(folder) / basename
                video.rename(destination)
                video = destination
                return True

            result = apply_manual_filename(database, "10", "Test Big Bunny (Alex Morgan).mp4", fake_move)
            self.assertEqual(result["status"], "renamed")
            scene["performers"] = []
            scene["files"][0]["path"] = str(video)
            refresh_scene_inventory(database, scene)
            preview = preview_scene_filename(database, "10")
            self.assertTrue(preview["proposed_path"].endswith("Test Big Bunny.mp4"))

    def test_numeric_filename_without_metadata_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            video = root / "123456.mp4"
            video.write_bytes(b"video")
            database = root / "inventory.sqlite3"
            scene = {"id": "10", "title": "", "studio": None, "performers": [],
                     "files": [{"id": "20", "path": str(video), "size": 5}]}
            inventory(database, [scene])
            summary, report = preview_safe_filenames(database)
            self.assertEqual(summary["unchanged"], 1)
            self.assertEqual(report[0]["base_stem"], "123456")
            self.assertEqual(report[0]["proposed_path"], str(video))

    def test_filename_preview_rebuilds_from_base_after_metadata_removal(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            video = root / "Original.mp4"
            video.write_bytes(b"video")
            database = root / "inventory.sqlite3"
            scene = {"id": "10", "title": "", "studio": {"name": "Studio"},
                     "performers": [{"id": "1", "name": "Person"}],
                     "files": [{"id": "20", "path": str(video), "size": 5}]}
            inventory(database, [scene])
            _, first = preview_safe_filenames(database)
            self.assertTrue(first[0]["proposed_path"].endswith("Original - Studio - Person.mp4"))
            scene["studio"] = None
            scene["performers"] = []
            inventory(database, [scene])
            _, second = preview_safe_filenames(database)
            self.assertEqual(second[0]["proposed_path"], str(video))

    def test_filename_preview_sanitizes_portable_illegal_characters(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            video = root / "original.mp4"
            video.write_bytes(b"video")
            database = root / "inventory.sqlite3"
            scene = {"id": "10", "title": 'Scene: A/B?', "studio": None, "performers": [],
                     "files": [{"id": "20", "path": str(video), "size": 5}]}
            inventory(database, [scene])
            _, report = preview_safe_filenames(database)
            self.assertTrue(report[0]["proposed_path"].endswith("Scene- A-B.mp4"))

    def test_single_scene_apply_uses_callback_and_moves_sidecar(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            video = root / "Original.mp4"
            sidecar = root / "Original.srt"
            video.write_bytes(b"video")
            sidecar.write_text("subtitle")
            database = root / "inventory.sqlite3"
            scene = {"id": "10", "title": "", "studio": {"name": "Studio"}, "performers": [],
                     "files": [{"id": "20", "path": str(video), "size": 5}]}
            inventory(database, [scene])
            preview_safe_filenames(database)
            preview = preview_scene_filename(database, "10")
            self.assertEqual(preview["status"], "ready")

            def fake_move(file_id, folder, basename):
                self.assertEqual(file_id, "20")
                video.rename(Path(folder) / basename)
                return True

            result = apply_scene_filename(database, "10", fake_move)
            self.assertTrue(result["action_performed"])
            self.assertTrue((root / "Original - Studio.mp4").exists())
            self.assertTrue((root / "Original - Studio.srt").exists())

    def test_single_scene_apply_moves_both_image_companion_styles(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            video = root / "Original.mp4"
            video.write_bytes(b"video")
            (root / "Original.jpg").write_bytes(b"stem image")
            (root / "Original.mp4.jpg").write_bytes(b"full-name image")
            (root / "Unrelated.jpg").write_bytes(b"leave alone")
            database = root / "inventory.sqlite3"
            inventory(database, [{"id": "10", "title": "Original", "studio": {"name": "Studio"},
                                  "performers": [], "files": [{"id": "20", "path": str(video), "size": 5}]}])
            preview_safe_filenames(database)

            def fake_move(_file_id, _folder, basename):
                video.rename(root / basename)
                return True

            result = apply_scene_filename(database, "10", fake_move)
            self.assertTrue(result["action_performed"])
            self.assertTrue((root / "Original - Studio.jpg").exists())
            self.assertTrue((root / "Original - Studio.mp4.jpg").exists())
            self.assertTrue((root / "Unrelated.jpg").exists())

    def test_image_companion_collision_blocks_all_renaming(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            video = root / "Original.mp4"
            video.write_bytes(b"video")
            (root / "Original.jpg").write_bytes(b"source")
            (root / "Original - Studio.jpg").write_bytes(b"occupied")
            database = root / "inventory.sqlite3"
            inventory(database, [{"id": "10", "title": "Original", "studio": {"name": "Studio"},
                                  "performers": [], "files": [{"id": "20", "path": str(video), "size": 5}]}])
            preview_safe_filenames(database)
            preview = preview_scene_filename(database, "10")
            self.assertEqual(preview["status"], "blocked")
            self.assertIn("Associated-file target already exists", preview["reason"])
            self.assertTrue(video.exists())

    def test_image_companions_roll_back_when_stash_rename_fails(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            video = root / "Original.mp4"
            video.write_bytes(b"video")
            image = root / "Original.mp4.jpg"
            image.write_bytes(b"image")
            database = root / "inventory.sqlite3"
            inventory(database, [{"id": "10", "title": "Original", "studio": {"name": "Studio"},
                                  "performers": [], "files": [{"id": "20", "path": str(video), "size": 5}]}])
            preview_safe_filenames(database)
            with self.assertRaises(RuntimeError):
                apply_scene_filename(database, "10", lambda *_args: None)
            self.assertTrue(image.exists())
            self.assertFalse((root / "Original - Studio.mp4.jpg").exists())

    def test_hook_refresh_uses_new_metadata_before_preview(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            video = root / "Original.mp4"
            video.write_bytes(b"video")
            database = root / "inventory.sqlite3"
            scene = {"id": "10", "title": "", "studio": None, "performers": [],
                     "files": [{"id": "20", "path": str(video), "size": 5}]}
            inventory(database, [scene])
            preview_safe_filenames(database)
            scene["performers"] = [{"id": "1", "name": "New Person"}]
            refresh_scene_inventory(database, scene)
            preview_safe_filenames(database)
            preview = preview_scene_filename(database, "10")
            self.assertTrue(preview["proposed_path"].endswith("Original - New Person.mp4"))

    def test_tag_only_refresh_does_not_change_naming_signature(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            video = root / "Original.mp4"
            video.write_bytes(b"video")
            database = root / "inventory.sqlite3"
            scene = {"id": "10", "title": "Title", "studio": {"name": "Studio"},
                     "performers": [{"id": "1", "name": "Person"}], "tags": [],
                     "files": [{"id": "20", "path": str(video), "size": 5}]}
            inventory(database, [scene])
            before = scene_naming_signature(database, "10")
            scene["tags"] = [{"id": "99", "name": "New Tag"}]
            refresh_scene_inventory(database, scene)
            self.assertEqual(before, scene_naming_signature(database, "10"))


if __name__ == "__main__":
    unittest.main()

"""SQLite schema definitions for Watchtower.

This module contains the complete base schema DDL and table definitions.
It is imported by migration and database initialisation services.
"""
from __future__ import annotations

from librarymanager_reconciliation import RECONCILIATION_SCHEMA

BASE_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS inventory_runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    stash_file_count INTEGER NOT NULL DEFAULT 0,
    stash_scene_count INTEGER NOT NULL DEFAULT 0,
    present_count INTEGER NOT NULL DEFAULT 0,
    missing_count INTEGER NOT NULL DEFAULT 0,
    changed_path_count INTEGER NOT NULL DEFAULT 0,
    restored_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running'
);
CREATE TABLE IF NOT EXISTS files (
    file_id TEXT PRIMARY KEY,
    scene_id TEXT NOT NULL,
    path TEXT NOT NULL,
    basename TEXT NOT NULL,
    title TEXT,
    studio TEXT,
    performers_json TEXT NOT NULL DEFAULT '[]',
    size INTEGER,
    duration REAL,
    height INTEGER,
    fingerprints_json TEXT NOT NULL DEFAULT '[]',
    scene_metadata_json TEXT NOT NULL DEFAULT '{}',
    exists_on_disk INTEGER NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    missing_since TEXT
);
CREATE TABLE IF NOT EXISTS inventory_events (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES inventory_runs(id),
    file_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    old_path TEXT,
    new_path TEXT,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_files_scene_id ON files(scene_id);
CREATE INDEX IF NOT EXISTS idx_files_path ON files(path);
CREATE INDEX IF NOT EXISTS idx_files_basename ON files(basename);
CREATE INDEX IF NOT EXISTS idx_events_run_id ON inventory_events(run_id);
CREATE TABLE IF NOT EXISTS reconciliation_runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    missing_count INTEGER NOT NULL DEFAULT 0,
    matched_count INTEGER NOT NULL DEFAULT 0,
    ambiguous_count INTEGER NOT NULL DEFAULT 0,
    unmatched_count INTEGER NOT NULL DEFAULT 0,
    skipped_folder_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running'
);
CREATE TABLE IF NOT EXISTS reconciliation_candidates (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES reconciliation_runs(id),
    file_id TEXT NOT NULL,
    scene_id TEXT NOT NULL,
    expected_path TEXT NOT NULL,
    candidate_path TEXT,
    confidence TEXT NOT NULL,
    reason TEXT NOT NULL,
    expected_size INTEGER,
    candidate_size INTEGER,
    oshash_match INTEGER,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_candidates_run_id ON reconciliation_candidates(run_id);
CREATE TABLE IF NOT EXISTS filename_state (
    file_id TEXT PRIMARY KEY REFERENCES files(file_id),
    base_stem TEXT NOT NULL,
    base_source TEXT NOT NULL,
    source_title TEXT,
    last_generated_stem TEXT,
    manual_studio TEXT,
    manual_performers_json TEXT NOT NULL DEFAULT '[]',
    managed_date TEXT,
    managed_quality TEXT,
    rename_protected INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS filename_preview_runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    examined_count INTEGER NOT NULL DEFAULT 0,
    proposed_count INTEGER NOT NULL DEFAULT 0,
    unchanged_count INTEGER NOT NULL DEFAULT 0,
    conflict_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running'
);
CREATE TABLE IF NOT EXISTS filename_previews (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES filename_preview_runs(id),
    file_id TEXT NOT NULL,
    scene_id TEXT NOT NULL,
    current_path TEXT NOT NULL,
    base_stem TEXT NOT NULL,
    proposed_path TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_filename_previews_run ON filename_previews(run_id);
CREATE TABLE IF NOT EXISTS file_checksum_cache (
    path TEXT PRIMARY KEY,
    file_id TEXT,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    mtime_ns INTEGER,
    device INTEGER,
    inode INTEGER,
    sha256 TEXT,
    oshash TEXT,
    status TEXT NOT NULL DEFAULT 'completed',
    calculated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_checksum_cache_size ON file_checksum_cache(size);
CREATE TABLE IF NOT EXISTS checksum_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    file_id TEXT,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    device INTEGER,
    inode INTEGER,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at REAL NOT NULL,
    claimed_at REAL,
    updated_at TEXT NOT NULL,
    last_error TEXT,
    UNIQUE(path, size, mtime_ns)
);
CREATE INDEX IF NOT EXISTS idx_checksum_jobs_due ON checksum_jobs(status,available_at,id);
CREATE TABLE IF NOT EXISTS rename_queue (
    scene_id TEXT PRIMARY KEY,
    enqueued_at REAL NOT NULL,
    available_at REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    processing_started_at REAL,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS rename_worker_state (
    id INTEGER PRIMARY KEY CHECK(id=1),
    scheduled INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO rename_worker_state(id,scheduled) VALUES (1,0);
CREATE TABLE IF NOT EXISTS rename_audit (
    id INTEGER PRIMARY KEY,
    scene_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    detail TEXT,
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER PRIMARY KEY,
    category TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'info',
    action TEXT NOT NULL,
    status TEXT NOT NULL,
    scene_id TEXT,
    file_id TEXT,
    old_path TEXT,
    new_path TEXT,
    detail TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_activity_recorded_at ON activity_log(recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_activity_scene_id ON activity_log(scene_id);
CREATE TABLE IF NOT EXISTS expected_moves (
    source_path TEXT NOT NULL,
    destination_path TEXT NOT NULL,
    expires_at REAL NOT NULL,
    PRIMARY KEY(source_path,destination_path)
);
CREATE TABLE IF NOT EXISTS expected_creates (
    path TEXT PRIMARY KEY,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS filesystem_events (
    event_key TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    source_path TEXT NOT NULL,
    destination_path TEXT,
    is_directory INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    event_count INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS transcoder_candidates (
    candidate_path TEXT PRIMARY KEY,
    source_path TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'waiting',
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_transcoder_candidates_source ON transcoder_candidates(source_path);
CREATE TABLE IF NOT EXISTS filesystem_monitor_status (
    id INTEGER PRIMARY KEY CHECK(id=1),
    token TEXT,
    pid INTEGER,
    state TEXT NOT NULL DEFAULT 'stopped',
    started_at TEXT,
    heartbeat_at TEXT,
    roots_json TEXT NOT NULL DEFAULT '[]',
    unavailable_roots_json TEXT NOT NULL DEFAULT '[]',
    active_moves_json TEXT NOT NULL DEFAULT '[]',
    auto_restart_attempted_at REAL,
    auto_restart_failures INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO filesystem_monitor_status(id,state) VALUES (1,'stopped');
CREATE TABLE IF NOT EXISTS incoming_files (
    path TEXT PRIMARY KEY,
    first_seen_at TEXT NOT NULL,
    last_checked_at TEXT NOT NULL,
    size INTEGER,
    modified_ns INTEGER,
    stable_since REAL,
    settle_seconds INTEGER NOT NULL DEFAULT 300,
    status TEXT NOT NULL DEFAULT 'waiting',
    attempts INTEGER NOT NULL DEFAULT 0,
    scan_job_id TEXT,
    detail TEXT,
    filing_diagnostic TEXT
);
CREATE INDEX IF NOT EXISTS idx_incoming_status ON incoming_files(status);
CREATE TABLE IF NOT EXISTS filesystem_reconciliation_runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    event_count INTEGER NOT NULL DEFAULT 0,
    verified_count INTEGER NOT NULL DEFAULT 0,
    review_count INTEGER NOT NULL DEFAULT 0,
    informational_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running'
);
CREATE TABLE IF NOT EXISTS filesystem_reconciliation_proposals (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES filesystem_reconciliation_runs(id),
    event_key TEXT NOT NULL,
    event_type TEXT NOT NULL,
    file_id TEXT,
    scene_id TEXT,
    old_path TEXT,
    new_path TEXT,
    confidence TEXT NOT NULL,
    recommendation TEXT NOT NULL,
    reason TEXT NOT NULL,
    action_performed INTEGER NOT NULL DEFAULT 0,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fs_proposals_run ON filesystem_reconciliation_proposals(run_id);
CREATE TABLE IF NOT EXISTS filing_incoming_baseline (
    path TEXT PRIMARY KEY,
    size INTEGER,
    modified_ns INTEGER,
    oshash TEXT,
    seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_filing_baseline_hash ON filing_incoming_baseline(oshash, size);
CREATE TABLE IF NOT EXISTS filing_baseline_acknowledgements (
    path TEXT PRIMARY KEY REFERENCES filing_incoming_baseline(path) ON DELETE CASCADE,
    reason TEXT NOT NULL DEFAULT 'intentionally_removed',
    acknowledged_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS filing_baseline_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    established_at TEXT NOT NULL,
    completed_at TEXT,
    incoming_folders_json TEXT NOT NULL DEFAULT '[]',
    file_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'in_progress',
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_filing_baseline_status ON filing_baseline_state(status);
CREATE TABLE IF NOT EXISTS filing_baseline_summary (
    id INTEGER PRIMARY KEY CHECK (id=1),
    initial_count INTEGER NOT NULL DEFAULT 0,
    remaining_count INTEGER NOT NULL DEFAULT 0,
    filed_count INTEGER NOT NULL DEFAULT 0,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    acknowledged_count INTEGER NOT NULL DEFAULT 0,
    completed_at TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS filing_destination_dir_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    root_path TEXT NOT NULL,
    dir_path TEXT NOT NULL,
    norm_name TEXT NOT NULL,
    depth INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_filing_dest_cache_root ON filing_destination_dir_cache(root_path);
CREATE INDEX IF NOT EXISTS idx_filing_dest_cache_norm ON filing_destination_dir_cache(norm_name);
CREATE TABLE IF NOT EXISTS filing_destination_cache_meta (
    root_path TEXT PRIMARY KEY,
    max_depth INTEGER NOT NULL,
    scanned_at TEXT NOT NULL,
    entry_count INTEGER NOT NULL,
    generation INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS filing_folder_mappings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    entity_name TEXT NOT NULL,
    folder_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(entity_type, entity_id)
);
CREATE INDEX IF NOT EXISTS idx_filing_mappings_entity ON filing_folder_mappings(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_filing_mappings_folder ON filing_folder_mappings(folder_path);
CREATE TABLE IF NOT EXISTS active_filing_transfers (
    proposal_id INTEGER PRIMARY KEY,
    scene_id TEXT,
    file_id TEXT,
    source_path TEXT NOT NULL,
    destination_path TEXT NOT NULL,
    destination_folder TEXT NOT NULL,
    stage TEXT NOT NULL,
    stage_label TEXT NOT NULL,
    detail TEXT NOT NULL,
    total_bytes INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS filing_transfer_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id INTEGER NOT NULL UNIQUE,
    target_destination_folder TEXT NOT NULL,
    target_entity_type TEXT,
    target_entity_id TEXT,
    update_metadata INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'queued',
    stash_job_id TEXT,
    enqueued_at TEXT NOT NULL,
    processing_started_at TEXT,
    finished_at TEXT,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_ftq_status ON filing_transfer_queue(status);
CREATE INDEX IF NOT EXISTS idx_ftq_proposal ON filing_transfer_queue(proposal_id);
CREATE TABLE IF NOT EXISTS filing_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id TEXT NOT NULL,
    scene_id TEXT NOT NULL,
    source_path TEXT NOT NULL,
    proposed_path TEXT NOT NULL,
    destination_folder TEXT NOT NULL,
    destination_filename TEXT NOT NULL,
    organize_by TEXT NOT NULL,
    matched_entity_id TEXT NOT NULL,
    matched_entity_name TEXT NOT NULL,
    matched_alias TEXT,
    match_source TEXT NOT NULL,
    reason TEXT NOT NULL,
    companions_json TEXT NOT NULL DEFAULT '[]',
    candidate_destinations_json TEXT NOT NULL DEFAULT '[]',
    is_custom_mapped INTEGER NOT NULL DEFAULT 0,
    in_nested_folder INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_filing_status ON filing_proposals(status);
CREATE INDEX IF NOT EXISTS idx_filing_file_id ON filing_proposals(file_id);
CREATE INDEX IF NOT EXISTS idx_filing_source_path ON filing_proposals(source_path);
CREATE TABLE IF NOT EXISTS duplicate_file_repairs (
    candidate_path TEXT PRIMARY KEY,
    candidate_file_id TEXT NOT NULL,
    scene_id TEXT NOT NULL,
    retained_file_id TEXT NOT NULL,
    retained_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    deleted_companions_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_duplicate_repairs_status ON duplicate_file_repairs(status);
CREATE TABLE IF NOT EXISTS unindexed_external_media (
    path TEXT PRIMARY KEY,
    basename TEXT,
    size INTEGER,
    discovered_at TEXT,
    ignored INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_unindexed_ignored ON unindexed_external_media(ignored);
"""

FULL_SCHEMA = BASE_SCHEMA + RECONCILIATION_SCHEMA
SCHEMA = FULL_SCHEMA

TABLE_NAMES = (
    "inventory_runs",
    "files",
    "inventory_events",
    "reconciliation_runs",
    "reconciliation_candidates",
    "filename_state",
    "filename_preview_runs",
    "filename_previews",
    "file_checksum_cache",
    "checksum_jobs",
    "rename_queue",
    "rename_worker_state",
    "rename_audit",
    "activity_log",
    "expected_moves",
    "expected_creates",
    "filesystem_events",
    "transcoder_candidates",
    "filesystem_monitor_status",
    "incoming_files",
    "filesystem_reconciliation_runs",
    "filesystem_reconciliation_proposals",
    "filing_incoming_baseline",
    "filing_baseline_acknowledgements",
    "filing_baseline_state",
    "filing_baseline_summary",
    "filing_destination_dir_cache",
    "filing_destination_cache_meta",
    "filing_folder_mappings",
    "active_filing_transfers",
    "filing_transfer_queue",
    "filing_proposals",
    "duplicate_file_repairs",
    "unindexed_external_media",
    "grouped_reconciliation_batches",
    "grouped_reconciliation_members",
    "grouped_reconciliation_event_links",
)

# Changelog

## 1.0.17 — 2026-09-23

- **Unresolved Filing Attention Actions**: Added **`✕ DISMISS ALERT`** to safely clear unresolved filing warnings from Needs Attention, plus direct **`⚡ EDIT WITH FASTTAG`** / **`🎬 OPEN SCENE`** buttons to assign missing metadata without leaving Watchtower.
- **Interactive Scene Navigation**: Replaced static text with interactive `[Scene ID]` pills on unresolved filing cards for one-click Stash scene access and FastTag right-click triggers.
- **Real-Time Backlog Tally**: Dismissed incoming videos now immediately increment the `📁 ORGANISE EXISTING FILES` header counter in real time, ensuring dismissed media remains trackable for later batch organization.
- **Expanded In-App User Guide & Safety Guidance**: Added dedicated sections for Automatic Filing and external file moves in Finder/Explorer, explicitly reinforcing safety checks, testing on small sample folders, and backup practices.

## 1.0.16 — 2026-09-23

- **Background Filing Queue**: Transformed proposal execution into non-blocking server-side background tasks via Stash's task system, allowing users to safely navigate away or queue multiple approvals without stalled or aborted transfers.
- **Interrupted Transfer Recovery**: Added automatic startup and periodic recovery to verify file positions on disk, reconcile Stash scene associations, and restore abandoned or stalled transfers without data loss.
- **Cross-Drive Companion Moves**: Fixed companion relocation across separate filesystem volumes and physical mount points (`shutil.move` cross-device stream fallback), preventing `[Errno 18] Cross-device link` errors.
- **OS Junk-File Filtering**: Excluded system metadata files (`.DS_Store`, `Thumbs.db`, `desktop.ini`) from incoming baseline scans and companion tracking so clean folders show zero remaining files.
- **Filing Approval & Navigation UX**: Enforced single-candidate destination selection, added in-place button confirmation with zero layout shift, auto-scrolled to progress and review proposals, and aligned search controls.

## 1.0.15 — 2026-09-22

- **On-Demand Disk Rescan**: Added **`⟳ Rescan Folders & Recalculate`** to the filing proposal actions menu (`⋮ ACTIONS`), immediately invalidating the 1-hour directory cache to discover newly created, deleted, or renamed folders on physical storage drives without waiting or restarting Stash.
- **Rescan Visual Feedback**: Added immediate in-progress scanning toast notifications, card inline loading spinner banners, and completion confirmation notices so disk sweep activity is always visible.
- **Custom Folder Mappings Filter & Scroll**: Bound the custom folder mappings list inside a scrollable container (`max-height: 340px`) with a real-time client-side search box matching name, entity type, and destination path, keeping `+ Add Custom Folder Mapping` controls persistently visible above.

## 1.0.14 — 2026-09-22

- **Backlog Organiser**: Preserve batch evaluation results and summary counts when re-evaluating individual items after metadata updates.
- **FastTag Keyboard Search**: Disable React-Bootstrap modal focus trap (`enforceFocus: false`) on the Organise Existing Files modal so FastTag search inputs retain focus and keyboard typing immediately upon opening.
- **Onboarding Wizard**: Bound wizard dialog to viewport height (`max-height: calc(100dvh - 3rem)`) with dedicated vertical scrolling on `.lm-wizard-body` and non-shrinking headers/footers, ensuring users with many Stash library roots can scroll Step 2 and always reach navigation buttons.

## 1.0.13 — 2026-09-22

### ⚡ External Moves & Folder Renames (Grouped Reconciliation)
- **Folder Rename & Move Detection**: External folder renames and moves in Finder, Windows Explorer, or scripts are automatically detected and coalesced into single grouped operations rather than generating dozens of individual missing-file errors.
- **Review-Driven Grouped Recovery**: Grouped folder moves and cross-volume relocations are presented in a unified Overview card showing source/destination paths, member counts, and verification status before any changes are committed.
- **Single-Pass Stash Reconciliation**: Once approved, Watchtower executes a single targeted directory scan in Stash, verifying member files by size and checksum/OSHash without full-library sweeps or hashing entire video bodies.
- **Cross-Volume Move Correlation**: Detects cross-device moves (which the OS manifests as delete+create pairs) by matching cryptographic and size fingerprints within a settling window.
- **Duplicate & Copy Group Safety**: Classifies copied folders as duplicate content (`folder_copy`) rather than moves, preserving original scene identities and requiring explicit user review before any new scene ingest.
- **Daemon Restart & Crash Resilience**: All grouped batches and member states are durably tracked in SQLite. Interrupted or in-flight reconciliations automatically recover and resume upon Watchtower restart.
- **Single-File Move Automation**: Unambiguous single-file moves on the same monitored library root continue to reconnect automatically in real-time.
- **Explicit Boundary Handling**: Documented behavior for offline mounts (gated until reconnect), externally re-encoded files (routed to partial review on checksum mismatch), and multi-destination folder splits.

### 📁 Backlog Organiser & Ingest Reliability (Work Package A)
- **Dynamic Backlog Working Set**: The Backlog Organiser dynamically tracks real-time unresolved files, retiring completed or moved items immediately.
- **Atomic Staged Snapshot Replacement**: Protection snapshot regeneration uses atomic staging to prevent baseline corruption if interrupted.
- **Incomplete Download Exclusion**: In-flight downloads (.part, .crdownload, .tmp) and active downloaders are cleanly excluded from backlog scans.
- **Unavailable Storage Root Handling**: Unmounted or offline drive roots display clear warning banners and disable destructive actions without crashing the monitor or losing state.
- **Cached Dynamic Directory Discovery**: Substantially accelerated folder discovery through intelligent TTL-bounded caching.

### 🏷️ Video Quality Filename Token (Work Package B)
- **Optional Canonical Quality Token**: Added `includeVideoQuality` option to embed standard resolution tags (e.g. `[1080p]`, `[720p]`, `[2160p]`) into filenames.
- **Configurable Token Placement**: Choose `start` (prefix) or `end` (suffix) positioning with responsive inline UI controls.
- **Zero-Rescan Ingestion Resolution**: Stored video dimensions from Stash's files table supply the resolution immediately for canonical filenames and read-only test previews without requiring full library rescans.

### 🛡️ Renaming & Filing Architecture Simplification
- **Decoupled Filing & Renaming**: Automatic Filing moves files to destination folders preserving original filenames without creating permanent rename protection locks.
- **Master Renaming Control**: Automatic Renaming toggle exclusively governs whether metadata edits in Stash trigger renames on disk.
- **Safe Schema Migration**: Clears legacy `rename_protected` flags safely in SQLite without enqueuing renames or triggering bulk renaming.
- **Cleaned Configuration**: Removed redundant `autoFilingPreserveFilename` switch from settings and UI.

### 🧹 Operational Retention & Maintenance
- **Bounded Retention Policy**: `prune_operational_records` bounds historical completed/resolved events while preserving actionable proposals and unreviewed issues indefinitely.


## 1.0.12 — 2026-09-21

- Isolate the native Windows filesystem monitor from Stash's console so plugin-operation cleanup cannot interrupt the watcher or terminate Stash.
- Replace Unix-style PID signal probes with non-destructive Windows process-handle checks, preventing liveness checks from terminating the watcher or an unrelated reused PID.
- Save Automatically Start Filesystem Monitor only after watcher startup succeeds, preventing failed starts from enabling an automatic retry loop.
- Preserve the existing detached monitor behaviour on macOS and Linux.

## 1.0.11 — 2026-09-19

- File-move reliability & transient retry: bounded retries with exponential backoff for transient filesystem permission and lock errors (EPERM, EACCES, EBUSY) during moved file verification, preserving moves for safe later retry rather than abandoning them.
- Rapid chained move support: follow persistent move history and verify final destination identity for rapid chained/consecutive moves (A -> B -> C).
- Safe NAS recovery: retain in-flight moves across network-share disconnects and recover unverified moves automatically on startup or root reconnection.
- Automatic companion resolution: automatically verify and resolve companion image sidecars (.jpg, .png, etc.) alongside reconnected videos upon successful Stash reconciliation.
- Monitor lifecycle logging: record MONITOR STARTED and MONITOR STOPPED events in activity history with timestamps, using process ownership and state transitions to prevent duplicate entries during plugin reloads.
- Reconnecting status & attention accuracy: distinguish in-flight file moves actively being processed by MoveWorker as "Reconnecting" (or "Waiting for retry") in HAPPENING NOW, eliminating false "Needs Attention" warnings.
- Early companion grace window: add a bounded 5-second grace period displaying "WAITING FOR VIDEO" for companion JPGs that arrive ahead of their video move.
- Strict ambiguity & failure visibility: ambiguous companions matching multiple videos and standalone JPGs without video association remain visible and actionable under Needs Attention, never hidden.

## 1.0.10 — 2026-09-17

- Non-blocking incoming scans: fallback checks, startup recovery, and network reconnect recovery never execute synchronous recursive `rglob()` on worker or caller threads.
- Bounded per-folder single-flight scan isolation: strictly at most one active scan thread per configured incoming folder, preventing thread growth or queue unboundedness.
- Independent folder failure isolation: if one network mount or incoming folder hangs during directory traversal, healthy incoming folders continue scanning independently.
- Non-blocking directory ingest (`submit_tree`): zero filesystem I/O before dispatch, ensuring directory create/move events never freeze the watchdog event loop on hung NAS mounts.
- Bounded directory scan locking: nested directory events are keyed to their owning configured incoming folder, sharing the single-flight scan lock.
- Process shutdown safety: all incoming scan threads run as daemon threads, ensuring unkillable kernel filesystem hangs never prevent clean daemon or Stash shutdown.

## 1.0.9 — 2026-09-17

- Add robust network-share disconnect and hang resilience for NAS/SMB/NFS mounts with non-blocking availability probing and thread pool isolation.
- Add bounded single-flight deletion scheduler preventing thread exhaustion during bulk or rapid deletion operations.
- Replace full inventory table scans with indexed range queries in find_scene_for_companion.
- Propagate notification preference hot-reloads dynamically without requiring a daemon restart.
- Enhance image companion pairing with explicit rules for generic artwork, ambiguous matches, and unrelated images, adding Review actions for Dismiss and Ignore.
- Seamlessly preserve single logical candidates across browser download lifecycles (Chrome .com.google.Chrome.* -> Unconfirmed *.crdownload -> .mp4, Firefox .part, Safari .download), eliminating intermediate fragmentation into false problem events.
- Strictly classify only true companion extensions as companions, preventing temporary download files from triggering false companion moves.
- Register temporary downloads completing outside incoming folders as newly created videos rather than uninventoried moves.


## 1.0.8 — 2026-09-15

- Bundle `watchdog` 6.0.0 with Watchtower so filesystem monitoring works in clean Stash Docker containers without a separate `pip install`.
- Keep the dependency private to the plugin so container and host Python installations are not modified.
- Add an isolated filesystem-event test that verifies the bundled package without access to system site-packages.
- Generate contact sheets with FFmpeg when ImageMagick is unavailable, using a bundled Roboto font and lossless intermediate images.

## 1.0.7 — 2026-09-14

- Warn users and require confirmation when enabling the beta Automatic Renaming feature.
- Add an optional Stash scene date to generated filenames, using the fixed sortable `YYYY-MM-DD` format.
- Allow the optional scene date at the beginning or end while keeping it disabled by default.
- Prevent duplicate dates by conservatively recognising an exact matching Stash date at title boundaries.
- Preserve previous managed dates so scene-date edits cannot produce both old and new dates in a filename.
- Correct connective-word boundary matching used when removing duplicated studio and performer metadata.
- Add focused date, collision, companion-file, compatibility and UI regression coverage.

## 1.0.6 — 2026-09-13

- Track plausible encoded replacements as neutral, persistent work in progress while the original remains available.
- Preserve overnight and batch transcoding candidates across Watchtower and Stash restarts without raising premature problem alerts.
- Reconcile only after the original disappears and the five-second decision window finds exactly one unowned candidate.
- Keep ambiguity and ownership conflicts fail-closed before scanning, after scanning and during the final inventory update.
- Let users explicitly review a candidate as an independent new file without cancelling other scheduled reconciliation work.
- Clear abandoned candidates when their encoded output disappears and expose failed reconciliation as an actionable review.

## 1.0.5 — 2026-09-13

- Fail closed when a transcoder replacement destination belongs to another file or scene.
- Consider every plausible same-folder replacement and refuse ambiguous candidates before scanning or reconnecting.
- Revalidate replacement ownership and uniqueness before scanning, after scanning and inside the final inventory transaction.
- Allow the detached filesystem monitor to reload itself when a plugin update replaces its code on disk.
- Treat Dismiss as applying to one filesystem-event occurrence so a later identical event becomes actionable again.

## 1.0.4 — 2026-09-13

- Prevent weak or ambiguous companion-file matches from moving artwork, subtitles or other sidecars to the wrong scene.
- Restrict weak companion matching to the video's directory while allowing only explicit, unique compound matches across directories.
- Preserve manual external video renames unless Stash reports an explicit naming-metadata change.
- Add opt-in compatibility for strong same-folder replacements created by FileFlows, HandBrake, Tdarr and similar transcoders.
- Support both delete-first and created-first transcoder replacement workflows, including extension changes and videos without companions.
- Keep strict size/hash move verification as the default and leave ambiguous replacement candidates for review.
- Prevent companion destination collisions from overwriting existing files and resolve stale create/delete events after confirmed reconnection.

## 1.0.3 — 2026-09-13

- Keep renamed videos and companion files together if Stash succeeds but Watchtower's local cache update fails, then recover the cache from Stash when possible.
- Enable SQLite foreign-key enforcement on every database connection.
- Repair pairing-title detection by replacing corrupt control characters with valid word-boundary matching.
- Measure abandoned rename work from processing start rather than original enqueue time.
- Make companion relocation for verified external moves preflighted and rollback-safe.
- Require explicit trust before executing a custom contact-sheet program, with executable validation and clearer guidance.
- Verify the monitor process command and unique token so a reused PID cannot be mistaken for Watchtower.

## 1.0.2 — 2026-09-13

- Recheck deleted paths immediately before removing a stale Stash scene.
- Refuse automatic removal for multi-file scenes, including files on offline storage.
- Show live file counts and percentage while onboarding builds its baseline inventory.
- Correct the remaining macOS-only startup wording in the help guide.
- Add executable onboarding-state, progress-formatting, and inventory-progress tests.

## 1.0.1 — 2026-09-13

- Make guided setup require an online Stash library root and a completed baseline inventory.
- Lock onboarding navigation while indexing, and provide clear retry guidance after failures.
- Prevent unsafe deletion of multi-file Stash scenes during filesystem review.
- Restrict bulk filesystem review to dismissal; corrective actions remain per-item.
- Harden Windows notification handling and protect startup connection data permissions.
- Correct multi-folder, cross-platform, database, and incoming-file guidance.
- Improve long activity-name wrapping and accessibility labels.
- Add automated UI and regression coverage for the new safeguards.

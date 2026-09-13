# Changelog

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

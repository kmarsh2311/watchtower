# Changelog

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

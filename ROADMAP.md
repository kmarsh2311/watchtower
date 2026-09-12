# Watchtower — Feature Roadmap & Backlog

This document tracks completed enhancements and future "nice to have" ideas for Watchtower.

---

## ✅ Completed & Verified

- **Auto-retry failed incoming downloads (#1):** Automatic retry queue and one-click batch retry buttons.
- **Activity log expiry (#2):** 5,000 row cap on `activity_log` to keep SQLite database lean and fast.
- **Monitor health heartbeat stale detection (#5):** Real-time process liveness and 30s heartbeat age checks with `● STALE` indicator and `⟳ RESTART WATCHER` button.
- **Instant Rename Preview during settle delay (#6):** Real-time proposed filename preview card with `[ ▶ RENAME NOW ]` and `[ ✕ CANCEL RENAME ]` actions.
- **Smart Revert Detection:** Automatically dequeues and dismisses pending rename countdown if an edit is reverted (`Proposed == Current`).
- **Critical Stability Fixes:** Process-level schema migration sentinels, safe `ALTER TABLE` race guards, 60-second stale `processing` rename recovery, non-square pixel DAR detection, and subprocess error reporting.

---

## 💡 Nice to Have / Future Exploration (Needs Testing First)

### 1. Pure `ffmpeg tile` Contact Sheet Generation (Zero ImageMagick Dependency)
- **Concept:** Every Stash installation already includes `ffmpeg` and `ffprobe`. By using `ffmpeg`'s built-in `tile` and `drawtext` filters (or Python Pillow), Watchtower could generate contact sheets without requiring users to install `ImageMagick` (`magick`).
- **Strategy:** If `ImageMagick` is present, use it for custom fonts/banners; if not, seamlessly fall back to pure `ffmpeg tile` so contact sheets work out of the box on all operating systems.
- **Testing needed:** Benchmark render speed, compare visual banner typography quality, and ensure compatibility across various video containers/codecs.

### 2. Cross-Platform Desktop Notifications (Linux & Windows)
- **Concept:** Expand native desktop notification support beyond macOS `osascript` to include Linux (`notify-send`) and Windows (PowerShell Action Center toasts).
- **Testing needed:** Test notification dispatch across Ubuntu/Debian/Arch desktop environments and Windows 10/11 shells.

### 3. Cross-Platform Auto-Start Services
- **Concept:** Provide one-click startup configuration for Linux (`systemd --user` or `~/.config/autostart`) and Windows (Startup folder / Task Scheduler) alongside macOS LaunchAgents.
- **Testing needed:** Permission handling across different Linux distros and Windows user account control (UAC).

### 4. Selective Dry-Run for Bulk Renames (#7)
- **Concept:** A multi-select checklist interface for library-wide bulk renames, allowing users to inspect and check/uncheck individual proposed renames before applying.
- **Testing needed:** Large library scaling (10k+ scenes) in the React virtualized list.

### 5. Multiple Incoming Watch Folders (#9)
- **Concept:** Allow configuring multiple incoming directories (e.g. across separate download drives or mount points) simultaneously.
- **Testing needed:** Watchdog multi-observer lifecycle management and per-folder settle timer tracking.

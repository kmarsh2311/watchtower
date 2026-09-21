<div align="center">

<img src="assets/watchtower-header.png" alt="Watchtower for Stash" width="100%" />

# 🗼 Watchtower for Stash
### The 24/7 Real-Time Filesystem Watcher, Automated Ingest Pipeline & Media Inventory Guardian for Stash

[![Version](https://img.shields.io/badge/version-1.0.13-00f0ff?style=for-the-badge)](https://github.com/kmarsh2311/watchtower/releases)
[![Stash](https://img.shields.io/badge/Stash-v0.26+-ff0055?style=for-the-badge)](https://github.com/stashapp/stash)
[![Python](https://img.shields.io/badge/Python-3.10+-39ff64?style=for-the-badge)](https://python.org)
[![License](https://img.shields.io/badge/license-AGPL--3.0-ffe600?style=for-the-badge)](LICENSE)
[![Architecture](https://img.shields.io/badge/Zero--Data--Loss-Guaranteed-00f0ff?style=for-the-badge)](#-zero-data-loss-philosophy)

<br />

<p align="center">
  <img src="assets/overview_terminal.png" alt="Watchtower Live Feed Terminal" width="100%" />
</p>

</div>

---

## ⚡ Overview

**Watchtower** is a 24/7 background system guardian and automated media ingest pipeline designed from the ground up for [Stash](https://github.com/stashapp/stash).

Instead of waiting for slow, manual library sweeps or wondering if external file moves broke your scene links, Watchtower continuously listens to your storage drives in real-time. Drop downloads into incoming folders and watch them automatically verify, settle, generate visual contact sheets, and import into Stash the moment they finish. Reorganize files in Finder or Explorer without fear—Watchtower detects moves by size and `OSHash` and reconnects scenes instantly without destructive rescans.

Built with a retro-futuristic Cyberpunk/Lighthouse control console, live event stream terminal, and strict **zero-data-loss** atomic safeguards, Watchtower gives you total visibility and automated control over your media collection.

---

## 🔥 Key Features

### 📡 1. 24/7 Live Storage Monitoring & Background Watcher
* **Continuous Real-Time Tracking:** Watchtower runs a silent, lightweight watchdog service that monitors all your configured Stash library roots simultaneously.
* **No More Manual Rescan Sweeps:** New files, modifications, deletions, and folder relocations are detected in real-time the moment they happen on your drives.
* **Quick-Peek Status Popover:** Click the glowing lighthouse in Stash's top navigation bar to check watcher health, pending changes, and folder states instantly from any page.
* **OS-Native Startup Daemon:** Keeps monitoring 24/7 without needing a web browser open:
  * **macOS:** Native LaunchAgent plist with protected tokens.
  * **Windows:** Silent background VBS runtime (`WshShell.Run`).
  * **Linux:** GNOME `.desktop` autostart daemon.
* **Self-Healing Heartbeat:** 2-second heartbeat loop, dead PID detection via `os.kill(pid, 0)`, and graceful token-authenticated control.

<p align="center">
  <img src="assets/filesystem_monitor.png" alt="Filesystem Monitor" width="100%" />
</p>

---

### 📥 2. Automated Multi-Folder Download Ingest & Settle Pipeline
* **Zero-Touch Ingest:** Configure up to 5 incoming download folders. Drop new videos in and let Watchtower handle everything from verification to import.
* **Partial Download Protection:** Actively filters out in-progress downloads (`.part`, `.partial`, `.crdownload`, `.download`, `.tmp`).
* **Dynamic Stability Settle Timers:** Watches file size and `mtime` continuously. The import timer (configurable from 1 to 30 mins) automatically resets if a transfer is still writing.
* **Targeted Automated Scans:** Once a video is 100% stable, Watchtower triggers an exact-path Stash scan with thumbnail, sprite, and perceptual-hash generation—importing only the new file in seconds.
* **Subfolder Discovery:** Automatically discovers and processes videos nested inside newly downloaded subfolders.

<p align="center">
  <img src="assets/navbar_status.png" alt="Watchtower Quick Status" width="45%" />
</p>

---

### ⚡ 3. External Moves, Folder Renames & Grouped Reconciliation
Watchtower tracks changes made externally in Finder, File Explorer, or command-line scripts, reconnecting Stash scenes without destructive rescans.

#### 🔄 What Happens When You Move or Rename Files Externally:

| Operation | Detection | Workflow | Verification & Safety |
| :--- | :--- | :--- | :--- |
| **Single File Move** *(Same Volume)* | Real-time watchdog `on_moved` event | **Automatic:** MoveWorker tracks and reconnects path in Stash | Size and `OSHash` cryptographic match; sidecars re-associated. |
| **Folder Rename** *(Populated Folder)* | Coalesced event waves derive old/new folder prefixes | **User Review:** Consolidated into a single Grouped Review card | Targeted Stash scan on destination prefix; all scene IDs & metadata preserved. |
| **Populated Folder Move** *(New Location)* | Prefix matching aggregates all member video events | **User Review:** Grouped move card with full member breakdown | Single-pass Stash scan; verifies all member files before marking complete. |
| **Cross-Volume Move** *(Between Disks)* | Delete+Create event correlation across roots | **User Review:** Matched by size and checksum cache within settling window | Reconnects Stash scene ID to new volume path once transfer settles. |
| **Folder / File Copy** *(Source Retained)* | Detected as new destination files with source still intact | **Review-Only:** Grouped as duplicate content (`folder_copy`) | **Zero scene theft:** Original scenes remain 100% untouched; copies require explicit decision. |
| **Daemon Restart Mid-Reconciliation** | Startup recovery reads durable SQLite state | **Automatic:** Resumes settling or re-attaches to in-flight Stash jobs | Zero dropped events or duplicate scans across restarts. |

#### 📋 Operational Modes Summary:
* **Fully Automatic**: Unambiguous single-file moves on active monitored library roots, companion sidecar re-linking, and settling timers.
* **Review & Approval Required**: Entire folder renames, multi-file folder relocations, cross-volume transfers, and duplicate copy groups.

#### ⚠️ Documented Edge Cases & Boundaries:
* **Offline / Unmounted Storage**: If a drive disconnects during an external move, Watchtower retains the in-flight state without destructive sweeps. Full reconciliation is gated until the drive reconnects.
* **External Re-encoding / Content Alteration**: If a file's binary content or size changes during an external move (e.g., external transcoding), `OSHash` and size verification will intentionally fail, and the item will be routed to `partial_review` rather than risking incorrect metadata assignment.
* **Multi-Destination Folder Splitting**: If a folder's contents are scattered across multiple disparate directories simultaneously, items that do not share a common destination prefix are evaluated as individual file movements rather than a single folder batch.

---

### 🖼️ 4. Visual Storyboard Contact Sheets (CSM)
* **Zero Stash Image Database Clutter:** High-resolution video storyboard sheets are saved directly beside the video file on your drive as companion files (`video_contact_sheet.jpg`)—visible in Finder/Explorer without bloating Stash's internal image library.
* **Smart 9:16 Vertical Video Reflow:** Automatically detects smartphone and social media vertical videos and optimizes the grid layout for standard widescreen viewing.
* **Custom Grid Layouts & Headers:** Choose 4x4 (16 frames), 5x4 (20 frames widescreen), or custom grids with detailed metadata header banners displaying resolution, file size, duration, and video codec.

<p align="center">
  <img src="assets/contact_sheets.png" alt="Contact Sheets CSM" width="100%" />
</p>

---

### 🛡️ 5. Atomic Renaming Engine & Sidecar Safety
* **Full Multi-Pass Preflight:** Before any file is modified on disk, Watchtower calculates proposed filenames, checks 255-byte filesystem boundaries, and verifies destination directories.
* **Synchronized Sidecar Renaming:** Subtitles (`.srt`, `.vtt`) and image artwork (`video.jpg`, `video.mp4.jpg`, `_contact_sheet.jpg`) rename in lockstep with the video.
* **Atomic Rollback Guarantee:** If Stash or the filesystem encounters an error mid-rename, all companion sidecars are restored to their original names in reverse dependency order.
* **Kernel-Level Lock Protection:** Cross-platform file locking (`fcntl` / `msvcrt`) eliminates race conditions between simultaneous Stash hooks.
* **Clean Conjunction Stripping:** Automatically cleans dangling connective words (`and`, `&`, `feat.`, `with`, `vs.`) and collapses multi-dashes without ever producing empty stems.

<p align="center">
  <img src="assets/filename_management.png" alt="Filename Management Sandbox" width="100%" />
</p>

---

### 🖥️ 6. Cyberpunk Control Terminal & Diagnostic Suite
* **Interactive Live Stream Terminal:** Real-time visual dashboard with CRT scanlines, glowing status pills, and live event monitoring.
* **250-Event Activity Flight Recorder:** Permanent audit trail of every rename, monitor event, download ingest, and recovery, exportable to JSON and CSV.
* **100% Read-Only Diagnostic Tools:** Built-in dry-run scanners for conflict resolution, metadata merge previews, and missing file candidate searches.
* **Interactive Simulation Sandbox:** Test custom naming formats against real scenes in your library with instant live previews before enabling automatic renames.
* **Native Desktop Notifications:** Desktop alerts on macOS, Windows, and Linux for important warnings, unavailable roots, and background failures.

---

## 📥 Installation

### Method 1: Stash Community Repository (Recommended)
1. In Stash, go to **Settings ➔ Plugins ➔ Available Plugins ➔ Add Source**.
2. Paste the Community Source URL:
   ```text
   https://kmarsh2311.github.io/my-stash-plugins/index.yml
   ```
3. Find **Watchtower** in the list and click **Install**.
4. Click **Reload Plugins**. The **📚** icon will appear in your Stash top navigation bar.

Watchtower includes its filesystem-monitoring dependency inside the plugin. Docker and NAS users do not need to install Python packages in their Stash container.
Contact sheets use ImageMagick when it is available and automatically fall back to FFmpeg with a bundled font when it is not.

### Method 2: Manual Install
1. Download [`librarymanager.zip`](https://kmarsh2311.github.io/my-stash-plugins/librarymanager.zip).
2. Extract the contents into your Stash plugins directory:
   * **Linux/macOS:** `~/.stash/plugins/librarymanager/`
   * **Windows:** `C:\Users\<Username>\.stash\plugins\librarymanager\`
3. Go to **Settings ➔ Plugins** and click **Reload Plugins**.

---

## 🚀 Quick Start Guide

1. **Guided Onboarding:** Click the **📚** icon in the Stash navigation bar to launch the 6-step setup wizard.
2. **Build Baseline Index:** In Step 4, click **⚡ Build Initial Inventory Now** to map your scenes into `librarymanager.sqlite3`.
3. **Configure Watched Folders:** Add your incoming download folder (e.g. `/Volumes/Media/Incoming`) to enable automated ingest.
4. **Enable 24/7 Monitoring:** Turn on **Start Monitoring with OS** to keep your library guarded 24/7.
5. **Tune Filename Rules:** Choose your preferred naming format and separators in the **Filename Management** tab.

---

## ⚙️ Configuration & Options

| Setting | Default | Description |
| :--- | :---: | :--- |
| **Start with OS** | `OFF` | Starts the background filesystem watcher on system boot / login. |
| **Automatic Incoming Scan** | `OFF` | Automatically imports finished downloads from incoming folders. |
| **Incoming Settle Minutes** | `5 min` | Time a video must remain unchanged before Stash imports it. |
| **Reconcile External Moves** | `OFF` | Reconnects scenes moved outside of Stash using size and OSHash verification. |
| **Generate Contact Sheets** | `OFF` | Creates video storyboard sheets on disk beside incoming videos. |
| **Auto-adjust Vertical Videos**| `ON` | Optimizes contact sheet layout for 9:16 vertical smartphone videos. |
| **Automatic Renaming** | `OFF` | Safely standardizes video and sidecar filenames on metadata edits. |
| **Desktop Notifications** | `ON` | Native system alerts for important warnings, moves, and failures. |

---

## 🔒 Zero Data Loss Philosophy

Watchtower is engineered to guarantee that your media collection is never damaged:
* **Safe Defaults:** All initial scans, inventory runs, and filename previews are strictly 100% read-only.
* **Isolated SQLite Database:** Watchtower maintains its own WAL-mode database (`librarymanager.sqlite3`) and never modifies Stash's internal SQLite database directly.
* **Stash Native Operations:** All primary file moves and renames are executed through Stash's native `moveFiles` GraphQL API, keeping Stash's internal path indices valid.
* **Symlink Safe:** Path containment checks resolve symlinks before testing folder boundaries, preventing accidental out-of-bounds operations.

---

## 🤝 Contributing & Support

* 🐛 **Bug Reports & Feature Requests:** Please open an issue on the [GitHub Issue Tracker](https://github.com/kmarsh2311/watchtower/issues).
* ☕ **Support:** If Watchtower saves you time managing your collection, feel free to [buy me a KitKat here 🍫](https://buymeacoffee.com/kamarsh)!

---

<div align="center">
  <sub>Built with ❤️ for the Stash Community.</sub>
</div>

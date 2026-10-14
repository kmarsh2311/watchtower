"""Video contact sheet generation service for Watchtower.

This module is responsible for:
- Discovering system fonts and bundled font assets for media annotation
- Probing video dimensions, display aspect ratios, and durations via FFprobe
- Extracting evenly spaced video frame samples across the duration
- Assembling contact sheet grids using ImageMagick montage or pure FFmpeg fallback
- Adding metadata header banners with resolution, duration, and file size details
- Handling custom external generator scripts with explicit trust safeguards
"""
from __future__ import annotations

import glob
import json
import logging
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

logger = logging.getLogger("librarymanager.services.contact_sheets")


def _get_plugin_root() -> Path:
    """Return the root directory of the Watchtower plugin."""
    return Path(__file__).resolve().parent.parent.parent


def _get_bundled_font_path() -> Path:
    """Return the path to the bundled contact-sheet font."""
    root_font = _get_plugin_root() / "assets" / "Roboto-Regular.ttf"
    if root_font.is_file():
        return root_font
    rel_font = Path(__file__).resolve().parent.parent.parent / "assets" / "Roboto-Regular.ttf"
    if rel_font.is_file():
        return rel_font
    return root_font


# Test patch-aware helpers delegating to librarymanager_core when monkeypatched
def _shutil_which(cmd: str) -> Optional[str]:
    core_mod = sys.modules.get("librarymanager_core")
    if core_mod and hasattr(core_mod, "shutil"):
        core_which = getattr(core_mod.shutil, "which", None)
        if core_which is not None and core_which is not shutil.which:
            return core_which(cmd)
    return shutil.which(cmd)


def _os_path_exists(path: Union[str, Path]) -> bool:
    core_mod = sys.modules.get("librarymanager_core")
    if core_mod and hasattr(core_mod, "os"):
        core_path = getattr(core_mod.os, "path", None)
        if core_path and hasattr(core_path, "exists"):
            core_exists = getattr(core_path, "exists")
            if core_exists is not os.path.exists:
                return bool(core_exists(path))
    return os.path.exists(path)


def _subprocess_run(*args, **kwargs):
    core_mod = sys.modules.get("librarymanager_core")
    if core_mod and hasattr(core_mod, "subprocess"):
        core_run = getattr(core_mod.subprocess, "run", None)
        if core_run is not None and core_run is not subprocess.run:
            return core_run(*args, **kwargs)
    return subprocess.run(*args, **kwargs)


def find_csm_font() -> str:
    """Locate a suitable font for contact sheet text annotation across supported platforms."""
    for f in [
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
        "/System/Library/Fonts/SFNS.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]:
        if _os_path_exists(f):
            return f
    return "Helvetica"


def format_csm_duration(seconds: Union[int, float]) -> str:
    """Format duration in seconds to MM:SS or HH:MM:SS format."""
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def ffmpeg_supports_drawtext(ffmpeg_bin: str) -> bool:
    """Check whether the given FFmpeg binary has drawtext filter support."""
    try:
        result = _subprocess_run(
            [ffmpeg_bin, "-hide_banner", "-h", "filter=drawtext"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    output = f"{result.stdout}\n{result.stderr}"
    return result.returncode == 0 and "Filter drawtext" in output


def generate_ffmpeg_contact_sheet_fallback(
    ffmpeg_bin: str,
    video_file: Path,
    dest_path: Path,
    tmp_dir: Path,
    timestamps: List[float],
    cols: int,
    rows: int,
    scale_w: int,
    include_banner: bool,
    width: int,
    height: int,
    file_size_str: str,
    formatted_duration: str,
    taskpolicy_prefix: List[str],
) -> Dict[str, Any]:
    """Render a contact sheet without ImageMagick using bundled font assets."""
    font_source = _get_bundled_font_path()
    if not font_source.is_file():
        return {"status": "error", "error": "Bundled contact-sheet font is missing"}
    if not ffmpeg_supports_drawtext(ffmpeg_bin):
        return {
            "status": "error",
            "error": "ImageMagick is unavailable and this FFmpeg build does not support the drawtext filter",
        }

    font_file = tmp_dir / "watchtower-font.ttf"
    shutil.copy2(font_source, font_file)
    errors = []
    frames = []
    for i, ts in enumerate(timestamps):
        label_file = tmp_dir / f"timestamp_{i:03d}.txt"
        label_file.write_text(format_csm_duration(ts), encoding="utf-8")
        frame_path = tmp_dir / f"frame_{i:03d}.png"
        command = taskpolicy_prefix + [
            ffmpeg_bin, "-y", "-ss", f"{ts:.2f}", "-i", str(video_file),
            "-an", "-sn", "-vf",
            (
                f"scale={scale_w}:-1,setsar=1,"
                f"drawtext=fontfile={font_file.name}:textfile={label_file.name}:"
                "fontsize=14:fontcolor=white:box=1:boxcolor=black@0.6:"
                "boxborderw=2:x=w-tw-12:y=h-th-12"
            ),
            "-frames:v", "1", "-compression_level", "3", str(frame_path),
        ]
        result = _subprocess_run(
            command, cwd=tmp_dir, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        if result.returncode == 0 and frame_path.is_file():
            frames.append(frame_path)
        else:
            errors.append(
                f"frame {i} @{ts:.1f}s: ffmpeg rc={result.returncode} "
                + result.stderr.decode(errors="replace").strip()[-100:]
            )

    if len(frames) < max(2, len(timestamps) // 2):
        detail = "; ".join(errors[:3]) if errors else "unknown"
        return {
            "status": "error",
            "error": f"Failed to extract enough frames ({len(frames)}/{len(timestamps)}): {detail}",
        }

    montage_path = tmp_dir / "montage.png"
    montage_command = taskpolicy_prefix + [
        ffmpeg_bin, "-y", "-framerate", "1", "-i", str(tmp_dir / "frame_%03d.png"),
        "-vf", f"tile={cols}x{rows}:padding=8:margin=4:color=0xF5F6F8",
        "-frames:v", "1", "-compression_level", "3", str(montage_path),
    ]
    montage_result = _subprocess_run(
        montage_command, cwd=tmp_dir, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    if montage_result.returncode != 0 or not montage_path.is_file():
        detail = montage_result.stderr.decode(errors="replace").strip()[-200:]
        return {"status": "error", "error": f"FFmpeg montage creation failed: {detail or 'unknown error'}"}

    final_filter = "format=yuvj420p"
    if include_banner:
        title_file = tmp_dir / "title.txt"
        info_file = tmp_dir / "info.txt"
        title_file.write_text(video_file.name, encoding="utf-8")
        info_file.write_text(
            f"Resolution: {width}x{height}  |  Size: {file_size_str}  |  Duration: {formatted_duration}",
            encoding="utf-8",
        )
        final_filter = (
            "pad=iw:ih+95:0:90:color=0xF5F6F8,"
            f"drawtext=fontfile={font_file.name}:textfile={title_file.name}:"
            "fontsize=18:fontcolor=0x2D3748:x=40:y=16,"
            f"drawtext=fontfile={font_file.name}:textfile={info_file.name}:"
            "fontsize=18:fontcolor=0x2D3748:x=40:y=50,format=yuvj420p"
        )

    final_path = tmp_dir / "contact-sheet.jpg"
    final_command = taskpolicy_prefix + [
        ffmpeg_bin, "-y", "-i", str(montage_path), "-vf", final_filter,
        "-frames:v", "1", "-q:v", "3", str(final_path),
    ]
    final_result = _subprocess_run(
        final_command, cwd=tmp_dir, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    if final_result.returncode != 0 or not final_path.is_file():
        detail = final_result.stderr.decode(errors="replace").strip()[-200:]
        return {"status": "error", "error": f"FFmpeg banner creation failed: {detail or 'unknown error'}"}

    shutil.copy2(final_path, dest_path)
    return {"status": "generated", "frames": len(frames), "renderer": "ffmpeg"}


_ORIGINAL_FFMPEG_FALLBACK = generate_ffmpeg_contact_sheet_fallback


def _is_mock_or_custom(fn: Any) -> bool:
    if fn is None:
        return False
    if hasattr(fn, "mock_calls") or hasattr(fn, "assert_called"):
        return True
    if not getattr(fn, "__dict__", {}).get("_is_watchtower_wrapper", False):
        return True
    return False


def _get_ffmpeg_fallback_fn() -> Callable:
    """Return the active ffmpeg fallback function, prioritizing any patch applied to librarymanager_core."""
    core_mod = sys.modules.get("librarymanager_core")
    if core_mod is not None:
        core_fn = getattr(core_mod, "generate_ffmpeg_contact_sheet_fallback", None)
        if _is_mock_or_custom(core_fn):
            return core_fn
    canon = sys.modules.get(__name__, None)
    canon_fn = getattr(canon, "generate_ffmpeg_contact_sheet_fallback", None) if canon else generate_ffmpeg_contact_sheet_fallback
    if canon_fn is not None and canon_fn is not _ORIGINAL_FFMPEG_FALLBACK:
        return canon_fn
    return _ORIGINAL_FFMPEG_FALLBACK


def generate_video_contact_sheet(
    video_path: Union[str, Path],
    output_path: Optional[Union[str, Path]] = None,
    grid: str = "4x4",
    include_banner: bool = True,
    adjust_vertical: bool = True,
    custom_script: Optional[str] = None,
    allow_custom_script: bool = False,
    overwrite: bool = False,
    logger: Any = None,
) -> Dict[str, Any]:
    """Generate a high-quality visual contact sheet for a video file."""
    video_file = Path(video_path).resolve()
    if not video_file.is_file():
        return {"status": "error", "error": f"Video not found: {video_file}"}

    # Standard companion file name: <video.ext>.jpg
    dest_path = Path(output_path) if output_path else Path(f"{video_file}.jpg")
    if dest_path.exists() and not overwrite:
        return {"status": "skipped", "message": "Contact sheet already exists", "path": str(dest_path)}

    # Custom scripts are trusted local executable code and require an explicit switch.
    if custom_script and allow_custom_script:
        script_path = Path(custom_script).expanduser().resolve()
        if not script_path.is_file():
            return {"status": "error", "error": f"Custom script is not a regular file: {script_path}"}
        if os.name != "nt" and not os.access(script_path, os.X_OK):
            return {"status": "error", "error": f"Custom script is not executable: {script_path}"}
        cmd = [str(script_path), str(video_file)]
        if _shutil_which("taskpolicy") or _os_path_exists("/usr/sbin/taskpolicy"):
            cmd = ["/usr/sbin/taskpolicy", "-b"] + cmd
        try:
            completed = _subprocess_run(cmd, capture_output=True, text=True, timeout=300)
            if completed.returncode:
                return {
                    "status": "error",
                    "error": f"Custom script exited with code {completed.returncode}: {(completed.stderr or '').strip()}",
                }
            if dest_path.exists() or Path(f"{video_file.stem}.jpg").exists():
                actual = dest_path if dest_path.exists() else Path(f"{video_file.stem}.jpg")
                return {"status": "generated", "path": str(actual), "custom_script": True}
            return {"status": "error", "error": "Custom script completed but did not create the expected contact sheet"}
        except Exception as e:
            return {"status": "error", "error": f"Custom script error: {e}"}

    # Locate ffmpeg, ffprobe and optional ImageMagick across supported systems.
    ffmpeg_bin = _shutil_which("ffmpeg") or ("/opt/homebrew/bin/ffmpeg" if _os_path_exists("/opt/homebrew/bin/ffmpeg") else "/usr/bin/ffmpeg" if _os_path_exists("/usr/bin/ffmpeg") else "ffmpeg")
    ffprobe_bin = _shutil_which("ffprobe") or ("/opt/homebrew/bin/ffprobe" if _os_path_exists("/opt/homebrew/bin/ffprobe") else "/usr/bin/ffprobe" if _os_path_exists("/usr/bin/ffprobe") else "ffprobe")
    magick_bin = _shutil_which("magick") or ("/opt/homebrew/bin/magick" if _os_path_exists("/opt/homebrew/bin/magick") else "/usr/bin/magick" if _os_path_exists("/usr/bin/magick") else "magick")

    ffmpeg_available = bool(_shutil_which(ffmpeg_bin) or _os_path_exists(ffmpeg_bin))
    ffprobe_available = bool(_shutil_which(ffprobe_bin) or _os_path_exists(ffprobe_bin))
    magick_available = bool(_shutil_which(magick_bin) or _os_path_exists(magick_bin))
    missing_required = [
        name for name, available in (("ffmpeg", ffmpeg_available), ("ffprobe", ffprobe_available))
        if not available
    ]
    if missing_required:
        return {"status": "error", "error": f"Required tool not found: {', '.join(missing_required)}"}

    # Probe metadata
    probe_cmd = [
        ffprobe_bin, "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,duration,display_aspect_ratio",
        "-show_entries", "format=duration,size",
        "-of", "json", str(video_file)
    ]
    try:
        p_res = _subprocess_run(probe_cmd, capture_output=True, text=True, timeout=30)
        p_data = json.loads(p_res.stdout)
    except Exception as e:
        return {"status": "error", "error": f"Could not probe video: {e}"}

    streams = p_data.get("streams", [{}])
    stream = streams[0] if streams else {}
    width = int(stream.get("width") or 1920)
    height = int(stream.get("height") or 1080)
    duration_str = stream.get("duration") or p_data.get("format", {}).get("duration") or "0"
    try:
        duration = float(duration_str)
    except (TypeError, ValueError):
        duration = 0.0

    if duration <= 10:
        return {"status": "skipped", "message": f"Video too short ({duration:.1f}s)"}

    # Parse grid
    try:
        parts = grid.lower().split("x")
        cols = int(parts[0])
        rows = int(parts[1])
    except Exception:
        cols, rows = 4, 4

    # Use Display Aspect Ratio when available — raw pixel dimensions are wrong
    # for videos with non-square pixels (e.g. 1440x1080 DVD rips with SAR 4:3).
    dar = stream.get("display_aspect_ratio") or ""
    try:
        dar_parts = [int(x) for x in dar.split(":")]
        display_ratio = dar_parts[0] / dar_parts[1] if len(dar_parts) == 2 and dar_parts[1] else None
    except (ValueError, ZeroDivisionError):
        display_ratio = None
    is_vertical = (display_ratio < 1.0) if display_ratio is not None else (height > width)
    if is_vertical and adjust_vertical:
        cols, rows = max(cols, 6), min(rows, 3)

    total_frames = cols * rows

    formatted_duration = format_csm_duration(duration)
    size_bytes = video_file.stat().st_size
    if size_bytes >= 1024**3:
        file_size_str = f"{size_bytes / (1024**3):.1f} GB"
    elif size_bytes >= 1024**2:
        file_size_str = f"{size_bytes / (1024**2):.1f} MB"
    else:
        file_size_str = f"{size_bytes / 1024:.0f} KB"

    font_path = find_csm_font()
    has_taskpolicy = bool(_shutil_which("taskpolicy") or _os_path_exists("/usr/sbin/taskpolicy"))
    taskpolicy_prefix = ["/usr/sbin/taskpolicy", "-b"] if has_taskpolicy else []

    # Frame sampling avoiding black intro & outro
    start_sec = duration * 0.05
    end_sec = duration * 0.92
    step_sec = (end_sec - start_sec) / max(1, total_frames - 1)
    timestamps = [start_sec + i * step_sec for i in range(total_frames)]

    scale_w = 460 if cols <= 4 else (376 if cols == 5 else 310)
    t_start = time.time()

    with tempfile.TemporaryDirectory(prefix="watchtower_csm_") as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)

        if not magick_available:
            fallback_fn = _get_ffmpeg_fallback_fn()
            fallback = fallback_fn(
                ffmpeg_bin, video_file, dest_path, tmp_dir, timestamps, cols, rows,
                scale_w, include_banner, width, height, file_size_str, formatted_duration,
                taskpolicy_prefix,
            )
            if fallback.get("status") != "generated":
                return fallback
            elapsed = time.time() - t_start
            return {
                "status": "generated",
                "path": str(dest_path),
                "grid": f"{cols}x{rows}",
                "frames": fallback["frames"],
                "duration": formatted_duration,
                "resolution": f"{width}x{height}",
                "elapsed_seconds": round(elapsed, 2),
                "renderer": "ffmpeg",
            }

        _csm_errors: list[str] = []
        for i, ts in enumerate(timestamps):
            frame_path = tmp_dir / f"frame_{i:03d}.jpg"
            cmd = taskpolicy_prefix + [
                ffmpeg_bin, "-y",
                "-ss", f"{ts:.2f}",
                "-i", str(video_file),
                "-an", "-sn",
                "-vf", f"scale={scale_w}:-1",
                "-vframes", "1",
                str(frame_path)
            ]
            _fr = _subprocess_run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            if _fr.returncode != 0 and not frame_path.is_file():
                _csm_errors.append(
                    f"frame {i} @{ts:.1f}s: ffmpeg rc={_fr.returncode} "
                    + _fr.stderr.decode(errors="replace").strip()[-100:]
                )

            if frame_path.is_file():
                ts_str = format_csm_duration(ts)
                stamp_cmd = taskpolicy_prefix + [
                    magick_bin, str(frame_path),
                    "-fill", "white",
                    "-font", font_path,
                    "-pointsize", "14",
                    "-gravity", "southeast",
                    "-undercolor", "rgba(0,0,0,0.6)",
                    "-annotate", "+12+12", ts_str,
                    str(frame_path)
                ]
                _sr = _subprocess_run(stamp_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                if _sr.returncode != 0:
                    _csm_errors.append(
                        f"stamp {i}: magick rc={_sr.returncode} "
                        + _sr.stderr.decode(errors="replace").strip()[-80:]
                    )

        frames = sorted(glob.glob(str(tmp_dir / "frame_*.jpg")))
        if len(frames) < max(2, total_frames // 2):
            err_detail = "; ".join(_csm_errors[:3]) if _csm_errors else "unknown"
            return {"status": "error",
                    "error": f"Failed to extract enough frames ({len(frames)}/{total_frames}): {err_detail}"}

        # Montage assembly
        temp_out = tmp_dir / "montage.jpg"
        montage_cmd = taskpolicy_prefix + [
            magick_bin, "montage"
        ] + frames + [
            "-background", "#F5F6F8",
            "-font", font_path,
            "-geometry", "+4+4",
            "-tile", f"{cols}x{rows}",
            "-strip", "-quality", "85",
            str(temp_out)
        ]
        _mr = _subprocess_run(montage_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        if not temp_out.is_file():
            _merr = _mr.stderr.decode(errors="replace").strip()[-200:]
            return {"status": "error",
                    "error": f"Montage creation failed: {_merr or 'unknown magick error'}"}

        # Banner addition
        if include_banner:
            id_cmd = [magick_bin, "identify", "-format", "%w", str(temp_out)]
            w_res = _subprocess_run(id_cmd, capture_output=True, text=True)
            try:
                sheet_w = int(w_res.stdout.strip())
            except Exception:
                sheet_w = 1840

            banner_h = 240 if is_vertical else 90
            banner_font_size = 36 if is_vertical else 18
            banner_splice = 40 if is_vertical else 24

            info_text = f"{video_file.name}\nResolution: {width}x{height}  |  Size: {file_size_str}  |  Duration: {formatted_duration}"

            banner_cmd = taskpolicy_prefix + [
                magick_bin,
                "-background", "#F5F6F8",
                "-fill", "#2D3748",
                "-font", font_path,
                "-pointsize", str(banner_font_size),
                "-size", f"{sheet_w - 24}x{banner_h}",
                "-gravity", "west",
                f"label:{info_text}",
                "-background", "#F5F6F8",
                "-splice", f"{banner_splice}x0",
                str(temp_out),
                "-append", "-strip", "-quality", "85",
                str(temp_out)
            ]
            _subprocess_run(banner_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

            footer_cmd = taskpolicy_prefix + [
                magick_bin, str(temp_out),
                "-size", f"{sheet_w}x5", "xc:#F5F6F8",
                "-append", "-strip", "-quality", "85",
                str(temp_out)
            ]
            _subprocess_run(footer_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        shutil.copy2(temp_out, dest_path)

    elapsed = time.time() - t_start
    return {
        "status": "generated",
        "path": str(dest_path),
        "grid": f"{cols}x{rows}",
        "frames": len(frames),
        "duration": formatted_duration,
        "resolution": f"{width}x{height}",
        "elapsed_seconds": round(elapsed, 2),
        "renderer": "imagemagick",
    }


__all__ = [
    "ffmpeg_supports_drawtext",
    "find_csm_font",
    "format_csm_duration",
    "generate_ffmpeg_contact_sheet_fallback",
    "generate_video_contact_sheet",
]

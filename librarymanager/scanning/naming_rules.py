"""Filename formatting, sanitization, metadata-stripping, date-parsing,
video-quality recognition, and proposal-stem generation logic for Watchtower.

This module owns:
- Pure filename formatting, sanitization, and proposal-stem algorithms
- Parsing and validating scene dates and video quality tokens
- Stripping managed studio/performer/date/quality metadata from titles and stems
- Preserving creative pairing titles on officially scraped scenes
- Filename state synchronization with database records
- Read-only preview generation for batch and single-scene renames
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

from librarymanager.database import create_connection, ensure_schema

logger = logging.getLogger("librarymanager.scanning.naming_rules")


def utc_now() -> str:
    """Return current UTC timestamp in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat()


def connect(database_path: Union[Path, str]) -> sqlite3.Connection:
    """Create a configured SQLite connection with active schema."""
    conn = create_connection(database_path)
    ensure_schema(conn, database_path)
    return conn


def _get_associated_file_target_fn() -> Optional[Callable]:
    """Dynamically resolve associated_file_target from core without hard circular import."""
    mod = sys.modules.get("librarymanager_core")
    if mod and hasattr(mod, "associated_file_target"):
        return getattr(mod, "associated_file_target")
    try:
        import librarymanager_core as core
        return getattr(core, "associated_file_target", None)
    except ImportError:
        return None


def _normalized_filename_text(value: str) -> str:
    return re.sub(r"[^\w]+", " ", str(value or "").casefold(), flags=re.UNICODE).strip()


def _compact_filename_text(value: str) -> str:
    return re.sub(r"[^\w]+", "", str(value or "").casefold(), flags=re.UNICODE)


def _same_filename_name(left: str, right: str) -> bool:
    """Treat spaced, compact and punctuation variants of a metadata name as equal."""
    return bool(_compact_filename_text(left)) and _compact_filename_text(left) == _compact_filename_text(right)


def _contains_name(text: str, name: str) -> bool:
    normalized_name = _normalized_filename_text(name)
    normalized_text = f" {_normalized_filename_text(text)} "
    if bool(normalized_name) and f" {normalized_name} " in normalized_text:
        return True
    compact_name = _compact_filename_text(name)
    return bool(compact_name) and compact_name in _compact_filename_text(text)


def _remove_legacy_studio(base: str, studio: str | None) -> str:
    """Remove an equivalent legacy [studio] or {studio} marker from a stable base."""
    if not studio:
        return base
    cleaned = re.sub(
        r"\s*[\[{]([^\[\]{}]+)[\]}]\s*",
        lambda match: " " if _same_filename_name(match.group(1), studio) else match.group(0),
        base,
    )
    return re.sub(r"\s+", " ", cleaned).strip(" -")


def _metadata_name_pattern(name: str, include_connectors: bool = False) -> str | None:
    """Return an exact-name regex that tolerates spaces/punctuation but not partial words."""
    parts = re.findall(r"\w+", str(name or ""), flags=re.UNICODE)
    if not parts:
        return None
    core = r"(?<!\w)" + r"[\W_]*".join(re.escape(part) for part in parts) + r"(?!\w)"
    if include_connectors:
        connectors = r"(?:\b(?:and|feat\.?|featuring|with|w/|vs\.?|versus|presents|in)\b|[&,+])"
        return rf"(?:{connectors}\s+)?{core}(?:\s+{connectors})?"
    return core


def _strip_managed_metadata(base: str, studios, performers, options: dict | None = None) -> str:
    """Remove only studio/performer names that Stash currently or previously identified as metadata."""
    opts = options or {}
    cleaned = str(base or "").strip()
    names = []
    seen = set()
    for value in list(studios or []) + list(performers or []):
        name = str(value or "").strip()
        key = _compact_filename_text(name)
        if name and key and key not in seen:
            seen.add(key)
            names.append(name)

    # Longest first prevents a shorter known name from consuming part of a longer one.
    names.sort(key=lambda value: len(_compact_filename_text(value)), reverse=True)
    strip_connectors = opts.get("stripConnectiveWords") is not False
    for name in names:
        pattern = _metadata_name_pattern(name, include_connectors=strip_connectors)
        if pattern:
            cleaned = re.sub(pattern, " ", cleaned, flags=re.IGNORECASE)

    # Remove empty legacy wrappers/groups and tidy punctuation left by extraction.
    cleaned = re.sub(r"\(\s*[,;&-]*\s*\)", " ", cleaned)
    cleaned = re.sub(r"\[\s*\]|\{\s*\}", " ", cleaned)
    cleaned = re.sub(r"\s*,\s*,+", ", ", cleaned)

    if opts.get("stripConnectiveWords") is not False:
        cleaned = re.sub(r"\s*[,;&+]*\s*(?:and|feat\.?|featuring|with|w/|vs\.?|versus|presents|in|&|\+)\s*[,;&+]*\s*-\s*", " - ", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*-\s*[,;&+]*\s*(?:and|feat\.?|featuring|with|w/|vs\.?|versus|presents|in|&|\+)\s*[,;&+]*\s*", " - ", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*-\s*[,;&+]*\s*(?:and|feat\.?|featuring|with|w/|vs\.?|versus|presents|in)?\s*[,;&+]*\s*(?=\s*[\(\[{]|$)", " ", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"(?i)\s+(?:feat\.?|featuring|with|and|w/|vs\.?|versus|presents|in|&|\+)\s*$", "", cleaned).strip(" -_,&")
        cleaned = re.sub(r"(?i)^\s*(?:feat\.?|featuring|with|and|w/|vs\.?|versus|presents|in|&|\+)\s+", "", cleaned).strip(" -_,&")
        if re.fullmatch(r"(?i)\s*(?:feat\.?|featuring|with|and|w/|vs\.?|versus|presents|in|&|\+|\-|,)+\s*", cleaned):
            cleaned = ""

    if opts.get("collapseMultipleDashes") is not False:
        cleaned = re.sub(r"(?:\s*-\s*){2,}", " - ", cleaned)

    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = cleaned.strip(" -_,&")
    return re.sub(r"\s+", " ", cleaned).strip()


def _remove_manual_metadata(base: str, studio: str | None, performers: list[str]) -> str:
    """Compatibility wrapper for older callers; may return an empty clean base."""
    return _strip_managed_metadata(base, [studio] if studio else [], performers)


def _is_officially_scraped(row) -> bool:
    """Return True if the scene has been matched to an external metadata provider (e.g. StashDB, The PornDB)."""
    if not row:
        return False
    try:
        meta_raw = row["scene_metadata_json"]
    except (IndexError, KeyError, TypeError):
        return False
    if not meta_raw:
        return False
    try:
        meta = json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
        if isinstance(meta, dict):
            return bool(meta.get("stash_ids") or meta.get("urls"))
    except (TypeError, ValueError):
        pass
    return False


def _should_strip_metadata_from_title(filename_options: dict | None, row=None, title: str | None = None) -> bool:
    if not filename_options:
        strip_enabled = True
    else:
        val = filename_options.get("stripMetadataFromTitle")
        if val is None:
            strip_enabled = True
        elif isinstance(val, bool):
            strip_enabled = val
        elif isinstance(val, str):
            strip_enabled = val.strip().lower() in ("true", "1", "yes", "on")
        else:
            strip_enabled = bool(val)

    if not strip_enabled:
        return False

    # Rule 1 (Refined): Protect creative pairing titles (e.g. "Jade and Xander", "Austin and LeGrand")
    # on officially scraped scenes so legitimate creative pairings are not mutilated.
    # If the title contains release delimiters (hyphens or bracketed tags), release metadata
    # is safely stripped to avoid duplicating studios or performers.
    if row is not None and _is_officially_scraped(row):
        check_title = str(title or "").strip()
        if not check_title and hasattr(row, "__getitem__"):
            try:
                check_title = str(row["title"] or "").strip()
            except Exception:
                pass
        if check_title:
            has_delimiters = bool(re.search(r"\s*-\s*", check_title) or re.search(r"\[.*?\]|\(.*?\)", check_title))
            is_pairing = bool(re.search(r"(?i)(?:\b(?:and|with|meets|vs)\b|&)", check_title))
            if is_pairing and not has_delimiters:
                return False

    return True


def _derive_blank_title_base(stem: str, studio: str | None, performers: list[str]) -> str:
    """Remove only exact trailing metadata produced by supported filename formats."""
    base = _remove_legacy_studio(stem.strip(), studio)
    if performers:
        match = re.search(r"\s*-\s*\(([^()]*)\)\s*$", base)
        if match:
            existing = {_normalized_filename_text(value) for value in match.group(1).split(",") if value.strip()}
            expected = {_normalized_filename_text(value) for value in performers if value.strip()}
            if existing == expected:
                base = base[:match.start()].rstrip()
        else:
            performer_suffix = ", ".join(name.strip() for name in performers if name.strip())
            plain_match = re.search(rf"\s*-\s*{re.escape(performer_suffix)}\s*$", base, flags=re.IGNORECASE)
            if plain_match:
                base = base[:plain_match.start()].rstrip()
    if studio:
        match = re.search(r"\s*-\s*\{([^{}]*)\}\s*$", base)
        if match and _same_filename_name(match.group(1), studio):
            base = base[:match.start()].rstrip()
        else:
            plain_match = re.search(rf"\s*-\s*{re.escape(studio)}\s*$", base, flags=re.IGNORECASE)
            if plain_match:
                base = base[:plain_match.start()].rstrip()
    base = re.sub(r"(?:\s*-\s*){2,}", " - ", base).strip(" -")
    return base or stem.strip()


def filename_format_options(config: dict | None = None) -> dict:
    """Turn the UI's friendly filename choices into validated formatting values."""
    config = config or {}
    order = [value.strip() for value in str(config.get("filenameOrder") or "title,studio,performers").split(",")]
    if sorted(order) != ["performers", "studio", "title"]:
        order = ["title", "studio", "performers"]
    section_separators = {"dash": " - ", "comma": ", ", "space": " ", "underscore": "_"}
    performer_separators = {"comma": ", ", "space": " ", "dash": " - ", "ampersand": " & "}
    return {
        "order": order,
        "date_position": "end" if str(config.get("filenameDatePosition") or "beginning").lower() == "end" else "beginning",
        "quality_position": "beginning" if str(config.get("filenameQualityPosition") or "end").lower() == "beginning" else "end",
        "section_separator": section_separators.get(str(config.get("filenameSectionSeparator") or "dash"), " - "),
        "performer_separator": performer_separators.get(str(config.get("filenamePerformerSeparator") or "comma"), ", "),
    }


def _validated_video_quality(quality: int | str | None) -> str:
    """Return canonical quality token like '[1080p]' or empty string."""
    if quality is None:
        return ""
    if isinstance(quality, int):
        return f"[{quality}p]" if quality > 0 else ""
    s = str(quality).strip()
    if not s:
        return ""
    m = re.match(r"^\[?(\d+)[pP]?\]?$", s)
    if m:
        try:
            val = int(m.group(1))
            return f"[{val}p]" if val > 0 else ""
        except ValueError:
            pass
    return ""


def _strip_matching_video_quality(text: str, quality: str | int | None) -> str:
    """Remove matching resolution token at a clear title boundary without guessing at other resolutions."""
    cleaned = str(text or "").strip()
    tok = _validated_video_quality(quality)
    if not cleaned or not tok:
        return cleaned
    num = tok.strip("[]pP")
    prefix = re.compile(rf"^(?:\[{num}[pP]\]|\({num}[pP]\)|{num}[pP])(?:\s*[-–—_,.:]+\s*|\s+|$)", re.IGNORECASE)
    suffix = re.compile(rf"(?:^|\s+|\s*[-–—_,.:]+\s*)(?:\[{num}[pP]\]|\({num}[pP]\)|{num}[pP])$", re.IGNORECASE)
    cleaned = prefix.sub("", cleaned, count=1)
    cleaned = suffix.sub("", cleaned, count=1)
    return cleaned.strip(" -–—_,.:")


def _row_video_quality(row) -> str:
    """Extract validated video quality token e.g. '[1080p]' from a files table row or record."""
    try:
        if not row:
            return ""
        row_dict = dict(row)
        height = row_dict.get("height")
        if height is not None:
            return _validated_video_quality(height)
    except (ValueError, TypeError):
        pass
    return ""


def _validated_scene_date(scene_date: str | None) -> str:
    try:
        value = str(scene_date or "").strip()
        return value if value and datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d") == value else ""
    except (TypeError, ValueError):
        return ""


def _matching_scene_date_variants(scene_date: str) -> list[str]:
    """Return supported textual forms of a known Stash date for exact deduplication only."""
    try:
        parsed = datetime.strptime(str(scene_date or ""), "%Y-%m-%d")
    except (TypeError, ValueError):
        return []
    values = [parsed.strftime(pattern) for pattern in ("%Y-%m-%d", "%d-%m-%Y", "%m-%d-%Y", "%Y-%d-%m")]
    variants = []
    for value in values:
        for separator in ("-", ".", "_"):
            candidate = value.replace("-", separator)
            if candidate not in variants:
                variants.append(candidate)
    return variants


def _strip_matching_scene_date(text: str, scene_date: str) -> str:
    """Remove the known scene date at a clear title boundary without guessing at other dates."""
    cleaned = str(text or "").strip()
    variants = _matching_scene_date_variants(scene_date)
    if not cleaned or not variants:
        return cleaned
    alternatives = "|".join(re.escape(value) for value in sorted(variants, key=len, reverse=True))
    prefix = re.compile(rf"^(?:{alternatives})(?:\s*[-–—_,.:]+\s*|\s+|$)", re.IGNORECASE)
    suffix = re.compile(rf"(?:^|\s+|\s*[-–—_,.:]+\s*)(?:{alternatives})$", re.IGNORECASE)
    cleaned = prefix.sub("", cleaned, count=1)
    cleaned = suffix.sub("", cleaned, count=1)
    return cleaned.strip(" -–—_,.:")


def _row_scene_date(row) -> str:
    """Return a validated ISO Stash scene date from an inventoried file row."""
    try:
        metadata = json.loads(row["scene_metadata_json"] or "{}")
        value = str(metadata.get("date") or "").strip()
        if value and datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d") == value:
            return value
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        pass
    return ""


def _sanitize_filename_stem(stem: str, options: dict | None = None) -> str:
    """Remove or replace illegal filesystem characters with clean punctuation."""
    opts = options or {}
    cleaned = str(stem or "").replace("\x00", "")  # strip null bytes first
    cleaned = re.sub(r'[?"*]', '', cleaned)
    cleaned = re.sub(r'[/\\:<>|]', '-', cleaned)
    cleaned = re.sub(r'\s*-\s*([!.,;])', r' \1', cleaned)
    if opts.get("collapseMultipleDashes") is not False:
        cleaned = re.sub(r'(?:\s*-\s*){2,}', ' - ', cleaned)
    return re.sub(r'\s+', ' ', cleaned).strip(' .')


def _is_only_metadata_or_connectors(text: str, studio: str | None, performers: list[str], options: dict | None = None) -> bool:
    """Check if text consists exclusively of studio, performers, and connective words/punctuation."""
    opts = options or {}
    cleaned = str(text or "").strip()
    if not cleaned:
        return True
    names = []
    if studio and str(studio).strip() and opts.get("stripStudioFromTitle") is not False:
        names.append(str(studio).strip())
    if opts.get("stripPerformersFromTitle") is not False:
        for p in performers:
            if p and str(p).strip():
                names.append(str(p).strip())
    names.sort(key=lambda s: len(s), reverse=True)
    for name in names:
        escaped = re.escape(name)
        cleaned = re.sub(rf"(?i)\b{escaped}\b", " ", cleaned)
        compact_escaped = "".join(re.escape(c) + r"\s*" for c in name if c.isalnum())
        if compact_escaped:
            cleaned = re.sub(rf"(?i)\b{compact_escaped}\b", " ", cleaned)

    connectors = r"(?i)\b(and|feat\.?|featuring|with|w/|vs\.?|versus|presents|in)\b|[&,+_–—\-\(\)\[\]\{\}\.\s]"
    cleaned = re.sub(connectors, " ", cleaned)
    return len(cleaned.strip()) == 0


def _proposed_stem(base: str, studio: str | None, performers: list[str], options: dict | None = None,
                   scene_date: str | None = None, previous_scene_date: str | None = None,
                   video_quality: str | int | None = None, previous_video_quality: str | int | None = None) -> str:
    """Build one canonical filename from the stored base + current Stash metadata."""
    formatting = filename_format_options(options)
    opts = options or {}

    title_val = str(base or "").strip()
    date_val = _validated_scene_date(scene_date) if opts.get("includeSceneDate") is True else ""
    if date_val:
        previous_date = _validated_scene_date(previous_scene_date)
        if previous_date and previous_date != date_val:
            title_val = _strip_matching_scene_date(title_val, previous_date)
        title_val = _strip_matching_scene_date(title_val, date_val)

    quality_val = _validated_video_quality(video_quality) if opts.get("includeVideoQuality") is True else ""
    if quality_val:
        if previous_video_quality:
            prev_quality = _validated_video_quality(previous_video_quality)
            if prev_quality and prev_quality != quality_val:
                title_val = _strip_matching_video_quality(title_val, prev_quality)
        title_val = _strip_matching_video_quality(title_val, quality_val)

    include_studio = opts.get("includeStudio") is not False
    studio_val = str(studio or "").strip() if include_studio else ""

    include_performers = opts.get("includePerformers") is not False
    raw_performers = [name.strip() for name in performers if str(name).strip()]
    max_perfs = int(opts.get("maxPerformersInFilename") or 0)
    if max_perfs > 0:
        raw_performers = raw_performers[:max_perfs]

    perf_val = formatting["performer_separator"].join(raw_performers) if include_performers else ""

    # Check granular title cleaning rules
    clean_perfs_only = opts.get("cleanPerformerOnlyTitles") is not False
    if clean_perfs_only and _is_only_metadata_or_connectors(title_val, studio_val, raw_performers, opts):
        title_val = ""
    else:
        studios_to_strip = [studio.strip()] if (opts.get("stripStudioFromTitle") is not False and studio and str(studio).strip()) else []
        perfs_to_strip = raw_performers if opts.get("stripPerformersFromTitle") is not False else []
        if studios_to_strip or perfs_to_strip:
            title_val = _strip_managed_metadata(title_val, studios_to_strip, perfs_to_strip, opts)

    values = {
        "title": title_val,
        "studio": studio_val,
        "performers": perf_val,
    }
    parts = [values[field] for field in formatting["order"] if values[field]]
    if date_val:
        parts.append(date_val) if formatting["date_position"] == "end" else parts.insert(0, date_val)
    if quality_val:
        if formatting["quality_position"] == "beginning":
            if date_val and formatting["date_position"] == "beginning":
                parts.insert(1, quality_val)
            else:
                parts.insert(0, quality_val)
        else:
            parts.append(quality_val)
    proposed = formatting["section_separator"].join(parts)
    return _sanitize_filename_stem(proposed, opts)


def _sync_filename_state(connection: sqlite3.Connection, state, row, current: Path, performers: list[str], filename_options: dict | None = None):
    """Synchronize the stable base with current Stash metadata and the physical filename."""
    opts = filename_options or {}
    master_source = str(opts.get("masterTitleSource") or "stash_title").lower()
    now = utc_now()
    raw_stash_title = str(row["title"] or "").strip()
    current_studio = str(row["studio"] or "").strip() or None

    try:
        previous_performers = json.loads(state["manual_performers_json"] or "[]")
    except (TypeError, ValueError):
        previous_performers = []
    previous_studio = str(state["manual_studio"] or "").strip() or None

    base = str(state["base_stem"] or "").strip()
    source = str(state["base_source"] or "filename")
    source_title = state["source_title"]

    previous_keys = {_compact_filename_text(name) for name in previous_performers if str(name).strip()}
    current_keys = {_compact_filename_text(name) for name in performers if str(name).strip()}
    studio_changed = not _same_filename_name(previous_studio or "", current_studio or "")
    performers_changed = previous_keys != current_keys
    metadata_changed = studio_changed or performers_changed

    last_generated = str(state["last_generated_stem"] or "").strip()
    externally_changed = bool(last_generated and current.stem != last_generated)

    strip_title = _should_strip_metadata_from_title(opts, row, raw_stash_title)
    studios_to_strip = [value for value in (previous_studio, current_studio) if value]
    performers_to_strip = list(previous_performers) + list(performers)

    if master_source == "filename":
        if metadata_changed or externally_changed or source != "filename":
            base = current.stem
            source = "filename"
            source_title = None
        base = _strip_managed_metadata(base, studios_to_strip, performers_to_strip, opts)
    elif master_source == "strict_title":
        source = "title"
        source_title = raw_stash_title
        if raw_stash_title:
            if strip_title:
                cleaned = _strip_managed_metadata(raw_stash_title, studios_to_strip, performers_to_strip, opts)
                base = cleaned if len(cleaned) >= 2 else raw_stash_title
            else:
                base = raw_stash_title
        else:
            base = ""
    else:  # "stash_title" (default)
        if raw_stash_title:
            source = "title"
            source_title = raw_stash_title
            if strip_title:
                cleaned = _strip_managed_metadata(raw_stash_title, studios_to_strip, performers_to_strip, opts)
                base = cleaned if len(cleaned) >= 2 else raw_stash_title
            else:
                base = raw_stash_title
        else:
            if metadata_changed or externally_changed or source == "title":
                base = current.stem
                source = "filename"
                source_title = None
            base = _strip_managed_metadata(base, studios_to_strip, performers_to_strip, opts)

    managed_date = str(state["managed_date"] or "").strip() or _row_scene_date(row)
    managed_quality = str(dict(state).get("managed_quality") or "").strip() or _row_video_quality(row)
    connection.execute(
        """UPDATE filename_state SET base_stem=?,base_source=?,source_title=?,manual_studio=?,
           manual_performers_json=?,managed_date=?,managed_quality=?,updated_at=? WHERE file_id=?""",
        (base, source, source_title, current_studio, json.dumps(performers, ensure_ascii=False),
         managed_date or None, managed_quality or None, now, row["file_id"]),
    )
    return connection.execute("SELECT * FROM filename_state WHERE file_id=?", (row["file_id"],)).fetchone()


def _create_filename_state(connection: sqlite3.Connection, row, current: Path, performers: list[str], filename_options: dict | None = None):
    """Create filename state from Stash title when known, otherwise the existing physical stem."""
    opts = filename_options or {}
    master_source = str(opts.get("masterTitleSource") or "stash_title").lower()
    now = utc_now()
    raw_stash_title = str(row["title"] or "").strip()
    studio = str(row["studio"] or "").strip() or None

    if master_source == "filename":
        title = ""
        source = "filename"
        raw_base = current.stem
    elif master_source == "strict_title":
        title = raw_stash_title
        source = "title"
        raw_base = raw_stash_title
    else:  # "stash_title" (default)
        title = raw_stash_title
        source = "title" if title else "filename"
        raw_base = title or current.stem

    strip_title = _should_strip_metadata_from_title(opts, row, raw_base)
    if source == "title" and title:
        if strip_title:
            cleaned = _strip_managed_metadata(title, [studio] if studio else [], performers, opts)
            clean_base = cleaned if len(cleaned) >= 2 else title
        else:
            clean_base = title
    elif raw_base:
        clean_base = _strip_managed_metadata(raw_base, [studio] if studio else [], performers, opts)
    else:
        clean_base = ""

    connection.execute(
        """INSERT INTO filename_state(file_id,base_stem,base_source,source_title,last_generated_stem,
           manual_studio,manual_performers_json,managed_date,managed_quality,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (row["file_id"], clean_base, source, title or None, None, studio,
         json.dumps(performers, ensure_ascii=False), _row_scene_date(row) or None,
         _row_video_quality(row) or None, now, now),
    )
    return connection.execute("SELECT * FROM filename_state WHERE file_id=?", (row["file_id"],)).fetchone()


def _refresh_manual_filename_state(connection: sqlite3.Connection, state, row, performers: list[str]):
    """Compatibility entry point: normalize any stored base using previous/current metadata."""
    current = Path(row["path"])
    return _sync_filename_state(connection, state, row, current, performers)


def scene_naming_signature(database_path: Path, scene_id: str, include_date: bool = False, include_quality: bool = False):
    """Return only metadata that is allowed to influence a filename."""
    connection = connect(database_path)
    try:
        row = connection.execute(
            "SELECT title,studio,performers_json,scene_metadata_json,height FROM files WHERE scene_id=? ORDER BY file_id LIMIT 1",
            (str(scene_id),),
        ).fetchone()
        if not row:
            return None
        signature = (row["title"] or "", row["studio"] or "", row["performers_json"] or "[]")
        if include_date:
            signature = signature + (_row_scene_date(row),)
        if include_quality:
            signature = signature + (_row_video_quality(row),)
        return signature
    finally:
        connection.close()


def preview_safe_filenames(database_path: Path, filename_options: dict | None = None) -> tuple[dict, list[dict]]:
    """Persist stable clean bases and calculate proposed paths; never rename a file."""
    now = utc_now()
    connection = connect(database_path)
    try:
        run_id = connection.execute("INSERT INTO filename_preview_runs(started_at) VALUES (?)", (now,)).lastrowid
        rows = connection.execute("SELECT * FROM files WHERE exists_on_disk=1 ORDER BY file_id").fetchall()
        provisional = []
        target_counts: Dict[str, int] = {}
        for row in rows:
            current = Path(row["path"])
            performers = json.loads(row["performers_json"] or "[]")
            state = connection.execute("SELECT * FROM filename_state WHERE file_id=?", (row["file_id"],)).fetchone()
            if state is None:
                state = _create_filename_state(connection, row, current, performers, filename_options)
            else:
                state = _sync_filename_state(connection, state, row, current, performers, filename_options)

            base = str(state["base_stem"] or "").strip()
            proposed_stem = _proposed_stem(base, row["studio"], performers, filename_options,
                                            _row_scene_date(row), state["managed_date"],
                                            _row_video_quality(row), dict(state).get("managed_quality"))
            # If all metadata and the clean base are empty, preserve the current stem rather than
            # proposing an invalid/empty filename.
            if not proposed_stem:
                proposed_stem = current.stem
            proposed = current.with_name(proposed_stem + current.suffix)
            normalized_target = os.path.normcase(os.path.abspath(proposed))
            target_counts[normalized_target] = target_counts.get(normalized_target, 0) + 1
            provisional.append((row, current, proposed, base, normalized_target))

        report = []
        summary = {"examined": len(provisional), "proposed": 0, "unchanged": 0, "conflicts": 0}
        for row, current, proposed, base, normalized_target in provisional:
            if len(proposed.name.encode("utf-8")) > 255:
                status, reason = "conflict", "Proposed filename exceeds 255 UTF-8 bytes"
            elif target_counts[normalized_target] > 1:
                status, reason = "conflict", "Safely skipped: Multiple files share the same filename target (collision protected)"
            elif proposed != current and proposed.exists():
                status, reason = "conflict", "A different filesystem entry already uses the target path"
            elif proposed == current:
                status, reason = "unchanged", "Current filename already matches the safe format"
            else:
                status, reason = "proposed", "Safe rename candidate"
            summary["conflicts" if status == "conflict" else status] += 1
            item = {"file_id": row["file_id"], "scene_id": row["scene_id"], "current_path": str(current),
                    "base_stem": base, "proposed_path": str(proposed), "status": status, "reason": reason,
                    "action_performed": False}
            report.append(item)
            connection.execute(
                "INSERT INTO filename_previews(run_id,file_id,scene_id,current_path,base_stem,proposed_path,status,reason,recorded_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, row["file_id"], row["scene_id"], str(current), base, str(proposed), status, reason, now),
            )
        connection.execute(
            "UPDATE filename_preview_runs SET completed_at=?,examined_count=?,proposed_count=?,unchanged_count=?,conflict_count=?,status='complete' WHERE id=?",
            (now, summary["examined"], summary["proposed"], summary["unchanged"], summary["conflicts"], run_id),
        )
        connection.commit()
        summary["run_id"] = run_id
        return summary, report
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def preview_scene_filename(
    database_path: Path,
    scene_id: str,
    filename_options: dict | None = None,
    associated_file_target_fn: Optional[Callable] = None,
) -> dict:
    """Return the latest calculated filename proposal for one scene."""
    connection = connect(database_path)
    try:
        rows = connection.execute(
            "SELECT * FROM files WHERE scene_id=? AND exists_on_disk=1 ORDER BY last_seen_at DESC, CAST(file_id AS INTEGER) DESC",
            (str(scene_id),),
        ).fetchall()
        row = None
        for r in rows:
            if Path(r["path"]).is_file():
                row = r
                break
        if not row and rows:
            row = rows[0]
        if not row:
            return {"scene_id": str(scene_id), "status": "blocked", "reason": "No present inventoried file for this scene"}

        current = Path(row["path"])
        performers = json.loads(row["performers_json"] or "[]")
        state = connection.execute("SELECT * FROM filename_state WHERE file_id=?", (row["file_id"],)).fetchone()
        if state is None:
            state = _create_filename_state(connection, row, current, performers, filename_options)
        else:
            state = _sync_filename_state(connection, state, row, current, performers, filename_options)
        connection.commit()

        proposed_stem = _proposed_stem(state["base_stem"], row["studio"], performers, filename_options,
                                        _row_scene_date(row), state["managed_date"],
                                        _row_video_quality(row), dict(state).get("managed_quality"))
        if not proposed_stem:
            proposed_stem = current.stem
        proposed = current.with_name(proposed_stem + current.suffix)
        if len(proposed.name.encode("utf-8")) > 255:
            status, reason = "blocked", "Proposed filename exceeds 255 UTF-8 bytes"
        elif proposed != current and proposed.exists():
            status, reason = "blocked", "Target path already exists"
        elif proposed == current:
            status, reason = "unchanged", "Current filename already matches"
        else:
            status, reason = "ready", "Single-scene rename passed preflight"
        sidecars = []
        target_fn = associated_file_target_fn or _get_associated_file_target_fn()
        if status == "ready" and current.parent.exists() and target_fn is not None:
            for candidate in current.parent.iterdir():
                if not candidate.is_file() or candidate == current:
                    continue
                target = target_fn(candidate, current, proposed)
                if target is None:
                    continue
                if target.exists():
                    status, reason = "blocked", f"Associated-file target already exists: {target.name}"
                    break
                sidecars.append({"source": str(candidate), "target": str(target)})
        return {"scene_id": str(scene_id), "file_id": row["file_id"], "current_path": str(current),
                "proposed_path": str(proposed), "base_stem": state["base_stem"], "status": status,
                "reason": reason, "associated_files": sidecars, "scene_date": _row_scene_date(row),
                "video_quality": _row_video_quality(row),
                "action_performed": False}
    finally:
        connection.close()


def preview_manual_filename(
    database_path: Path,
    scene_id: str,
    requested_name: str,
    associated_file_target_fn: Optional[Callable] = None,
) -> dict:
    """Preflight one exact user-supplied basename without changing anything."""
    requested_name = str(requested_name or "").strip()
    if not requested_name or Path(requested_name).name != requested_name or requested_name in (".", ".."):
        return {"scene_id": str(scene_id), "status": "blocked", "reason": "Enter a filename, not a path",
                "action_performed": False}
    connection = connect(database_path)
    try:
        row = connection.execute("SELECT * FROM files WHERE scene_id=? AND exists_on_disk=1 ORDER BY file_id LIMIT 1",
                                 (str(scene_id),)).fetchone()
        if not row:
            return {"scene_id": str(scene_id), "status": "blocked", "reason": "No present inventoried file for this scene",
                    "action_performed": False}
        current = Path(row["path"])
        supplied = Path(requested_name)
        if supplied.suffix and supplied.suffix.casefold() != current.suffix.casefold():
            return {"scene_id": str(scene_id), "file_id": row["file_id"], "current_path": str(current),
                    "status": "blocked", "reason": f"The video extension must remain {current.suffix}", "action_performed": False}
        stem = supplied.stem if supplied.suffix else requested_name
        stem = _sanitize_filename_stem(stem)
        proposed = current.with_name(stem + current.suffix)
        if not stem:
            status, reason = "blocked", "The filename cannot be empty"
        elif len(proposed.name.encode("utf-8")) > 255:
            status, reason = "blocked", "Proposed filename exceeds 255 UTF-8 bytes"
        elif proposed != current and proposed.exists():
            status, reason = "blocked", "Target path already exists"
        elif proposed == current:
            status, reason = "unchanged", "The filename already matches"
        else:
            status, reason = "ready", "Manual correction passed preflight"
        sidecars = []
        target_fn = associated_file_target_fn or _get_associated_file_target_fn()
        if status == "ready" and target_fn is not None and current.parent.exists():
            for candidate in current.parent.iterdir():
                if not candidate.is_file() or candidate == current:
                    continue
                target = target_fn(candidate, current, proposed)
                if target is None:
                    continue
                if target.exists():
                    status, reason = "blocked", f"Associated-file target already exists: {target.name}"
                    break
                sidecars.append({"source": str(candidate), "target": str(target)})
        return {"scene_id": str(scene_id), "file_id": row["file_id"], "current_path": str(current),
                "proposed_path": str(proposed), "status": status, "reason": reason,
                "associated_files": sidecars, "metadata_studio": row["studio"],
                "metadata_performers": json.loads(row["performers_json"] or "[]"), "action_performed": False}
    finally:
        connection.close()


__all__ = [
    "connect",
    "filename_format_options",
    "preview_manual_filename",
    "preview_safe_filenames",
    "preview_scene_filename",
    "scene_naming_signature",
    "utc_now",
    "_compact_filename_text",
    "_contains_name",
    "_create_filename_state",
    "_derive_blank_title_base",
    "_is_officially_scraped",
    "_is_only_metadata_or_connectors",
    "_matching_scene_date_variants",
    "_metadata_name_pattern",
    "_normalized_filename_text",
    "_proposed_stem",
    "_refresh_manual_filename_state",
    "_remove_legacy_studio",
    "_remove_manual_metadata",
    "_row_scene_date",
    "_row_video_quality",
    "_same_filename_name",
    "_sanitize_filename_stem",
    "_should_strip_metadata_from_title",
    "_strip_managed_metadata",
    "_strip_matching_scene_date",
    "_strip_matching_video_quality",
    "_sync_filename_state",
    "_validated_scene_date",
    "_validated_video_quality",
]

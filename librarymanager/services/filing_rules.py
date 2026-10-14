"""Filing rules, entity matching, custom folder mappings, and destination resolution service.

This module is responsible for:
- Conservative alias & canonical name matching for performers, studios, and tags
- Resolution of destination roots, subdirectories, and depth bounding
- Custom folder mappings management (get, save, delete)
- In-memory and database-backed destination directory discovery caching
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

from librarymanager.database.connection import create_connection
from librarymanager.database.migrations import ensure_schema
from librarymanager.scanning.filesystem import _is_subpath_of, is_actionable_incoming_file
from librarymanager.scanning.reconciliation import parse_cold_storage_roots

logger = logging.getLogger("librarymanager.services.filing_rules")

__all__ = [
    "NOISE_ALIASES",
    "match_performer_for_filing",
    "match_studio_for_filing",
    "get_configured_filing_destination_roots",
    "get_filing_folder_mappings",
    "save_filing_folder_mapping",
    "delete_filing_folder_mapping",
    "invalidate_incoming_discovery_cache",
    "discover_incoming_files_cached",
    "invalidate_destination_dir_cache",
    "refresh_destination_dir_cache",
    "resolve_filing_destinations",
    "resolve_tag_filing_destinations",
    "resolve_filing_destination_folder",
]


def utc_now() -> str:
    """Return the current UTC timestamp in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_mock(fn: Any) -> bool:
    if fn is None:
        return False
    return (
        hasattr(fn, "mock_calls")
        or hasattr(fn, "assert_called")
        or type(fn).__name__ in ("Mock", "MagicMock", "AsyncMock")
    )


def _connect(database_path: Path) -> sqlite3.Connection:
    connection = create_connection(database_path)
    ensure_schema(connection, database_path)
    return connection


def _get_core_fn(name: str, fallback_fn: Optional[Callable] = None) -> Optional[Callable]:
    """Retrieve an active override or mock, prioritizing direct patches on this module."""
    if _is_mock(fallback_fn):
        return fallback_fn

    core_mod = sys.modules.get("librarymanager_core")
    if core_mod is not None:
        core_fn = getattr(core_mod, name, None)
        if core_fn is not None:
            if _is_mock(core_fn):
                return core_fn
            if not getattr(core_fn, "_is_watchtower_wrapper", False) and name in (
                "connect",
                "_connect",
                "record_activity",
                "_scan_destination_subdirectories",
            ):
                return core_fn

    return fallback_fn


def connect(database_path: Path) -> sqlite3.Connection:
    conn_fn = _get_core_fn("connect", _connect)
    return conn_fn(database_path)


def record_activity(
    database_path: Path,
    category: str,
    action: str,
    status: str,
    *,
    severity: str = "info",
    scene_id: str | None = None,
    file_id: str | None = None,
    old_path: str | None = None,
    new_path: str | None = None,
    detail: str = "",
    metadata: dict | None = None,
):
    core_fn = _get_core_fn("record_activity", None)
    if core_fn is not None:
        return core_fn(
            database_path,
            category,
            action,
            status,
            severity=severity,
            scene_id=scene_id,
            file_id=file_id,
            old_path=old_path,
            new_path=new_path,
            detail=detail,
            metadata=metadata,
        )

    connection = connect(database_path)
    try:
        connection.execute(
            """INSERT INTO activity_log(category,severity,action,status,scene_id,file_id,old_path,new_path,
                   detail,metadata_json,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                category,
                severity,
                action,
                status,
                str(scene_id) if scene_id is not None else None,
                str(file_id) if file_id is not None else None,
                old_path,
                new_path,
                detail,
                json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
                utc_now(),
            ),
        )
        connection.execute(
            """DELETE FROM activity_log WHERE id NOT IN (
                   SELECT id FROM activity_log ORDER BY id DESC LIMIT 5000
               )"""
        )
        connection.commit()
    finally:
        connection.close()


NOISE_ALIASES = {
    "the", "and", "or", "in", "on", "at", "to", "for", "of", "with", "by",
    "a", "an", "all", "is", "it", "this", "that", "from", "into",
    "hd", "4k", "1080p", "720p", "2160p", "uhd", "sd", "dvd", "bluray", "rip",
    "scene", "part", "video", "clip", "movie", "film", "show", "series", "episode",
    "star", "stars", "girl", "girls", "boy", "boys", "man", "men", "guy", "guys",
    "top", "best", "hot", "live", "club", "vip", "pro", "new", "raw", "cut",
    "xxx", "porn", "sex", "action", "vr", "bonus", "extra", "trailer"
}


def _build_token_regex(phrase: str) -> re.Pattern | None:
    tokens = [re.escape(t) for t in re.split(r'[\s_\-\.]+', phrase.strip()) if t]
    if not tokens:
        return None
    pattern_str = r'(?<![a-zA-Z0-9])' + r'[\s_\-\.]+'.join(tokens) + r'(?![a-zA-Z0-9])'
    return re.compile(pattern_str, re.IGNORECASE)


def _single_token_alias_is_standalone(target: str, start: int, end: int) -> bool:
    """Accept a one-word alias only when it occupies a complete filename/title segment.

    This prevents an alias such as ``Sam`` from claiming the distinct apparent
    name ``Sam Taylor``. Multi-word aliases retain the normal token matching
    rules. Segments are separated by common filename credit delimiters.
    """
    left = target[:start]
    right = target[end:]
    left_parts = re.split(r'\s+(?:[-–—&,+|/\\])\s+|[\(\)\[\]\{\};|]', left)
    right_parts = re.split(r'\s+(?:[-–—&,+|/\\])\s+|[\(\)\[\]\{\};|]', right)
    segment = f"{left_parts[-1] if left_parts else ''}{target[start:end]}{right_parts[0] if right_parts else ''}"
    return len(re.findall(r"[A-Za-z0-9]+", segment)) == 1


def _normalize_name_for_folder_match(s: str) -> str:
    return re.sub(r'[\s_\-\.]+', ' ', str(s or "")).strip().lower()


def _compact_name_for_folder_match(s: str) -> str:
    """Remove supported separators for conservative joined-word folder matching."""
    return re.sub(r'[\s_\-\.]+', '', str(s or "")).strip().lower()


def _strip_conservative_folder_descriptors(norm_name: str) -> str:
    """Conservatively strip known prefix/suffix descriptors ('The ', ' Collection', 'The ... Collection')
    and parenthetical/bracketed annotations to support folder patterns like
    'The Cole Bentley Collection' or 'The Kyle Polaski (Michal Stranik, Damien Porch) Collection'
    without arbitrary fuzzy matching."""
    s = norm_name.strip()
    if s.startswith("the "):
        s = s[4:].strip()
    if s.endswith(" collection"):
        s = s[:-11].strip()
    s_no_paren = re.sub(r'[\(\[\{].*?[\)\]\}]', '', s).strip()
    s_no_paren = re.sub(r'\s+', ' ', s_no_paren)
    return s_no_paren or s


def _filter_subsumed_matches(raw_matches: list[dict]) -> dict[str, dict]:
    """Prune matches that are strictly subsumed by longer canonical matches,
    or where a canonical name match shares the exact same span as an alias.
    Preserves ambiguity when genuinely separate entities match, or when conflicting
    identities match the same span."""
    surviving = []
    for m in raw_matches:
        s, e = m["span"]
        subsumed = False
        for other in raw_matches:
            if other["entity"]["id"] == m["entity"]["id"]:
                continue
            os, oe = other["span"]

            # If other is a canonical-name match strictly enclosing m (e.g. shorter alias or subtoken)
            if other["is_name"] and os <= s and oe >= e and (oe - os) > (e - s):
                subsumed = True
                break

            # If both are aliases and other strictly encloses m
            if not m["is_name"] and os <= s and oe >= e and (oe - os) > (e - s):
                subsumed = True
                break

            # Exact same span: prefer complete canonical name over alias
            if os == s and oe == e and other["is_name"] and not m["is_name"]:
                subsumed = True
                break

        if not subsumed:
            surviving.append(m)

    unique = {}
    for m in surviving:
        ent_id = m["entity"]["id"]
        if ent_id not in unique or (m["is_name"] and not unique[ent_id]["is_name"]):
            unique[ent_id] = m
    return unique


def match_performer_for_filing(scene: dict, filename: str, all_performers: list[dict], match_source: str = "metadata_first") -> dict:
    """Conservatively match performers for automatic filing.
    - An uncertain entity match must NEVER produce a proposal.
    - Reliably parses single or multiple distinct co-starring performers.
    - Prefer complete canonical-name matches over shorter aliases contained within the same span.
    - Preserves ambiguity when conflicting identities match the exact same or overlapping text span."""
    if match_source in ("metadata_first", "metadata_only"):
        scene_performers = scene.get("performers") or []
        if len(scene_performers) >= 1:
            p_list = [{"id": str(p["id"]), "name": p["name"]} for p in scene_performers if p.get("id") and p.get("name")]
            if p_list:
                return {
                    "matched": True,
                    "entities": p_list,
                    "entity": p_list[0],
                    "matched_alias": None,
                    "source": "metadata"
                }
        elif match_source == "metadata_only":
            return {"matched": False, "reason": "No performer tagged in scene metadata"}

    target_str = f"{Path(filename).stem} | {scene.get('title') or ''}".strip(" |")
    if not target_str:
        return {"matched": False, "reason": "No filename or title to match"}

    alias_owner = {}
    ambiguous_aliases = set()
    for p in all_performers:
        p_id = str(p.get("id"))
        aliases = p.get("alias_list") or []
        for alias in aliases:
            a_norm = alias.strip().lower()
            if not a_norm:
                continue
            if a_norm in alias_owner and alias_owner[a_norm] != p_id:
                ambiguous_aliases.add(a_norm)
            else:
                alias_owner[a_norm] = p_id

    raw_matches = []
    for p in all_performers:
        p_id = str(p.get("id"))
        p_name = (p.get("name") or "").strip()
        if not p_name:
            continue

        name_norm = p_name.lower()
        if len(name_norm) >= 3 and name_norm not in NOISE_ALIASES:
            rx = _build_token_regex(p_name)
            if rx:
                for m in rx.finditer(target_str):
                    raw_matches.append({
                        "entity": {"id": p_id, "name": p_name},
                        "matched_alias": None,
                        "span": (m.start(), m.end()),
                        "is_name": True
                    })

        aliases = p.get("alias_list") or []
        for alias in aliases:
            a_norm = alias.strip().lower()
            if len(a_norm) < 3 or a_norm in NOISE_ALIASES or a_norm in ambiguous_aliases:
                continue
            rx = _build_token_regex(alias)
            if rx:
                for m in rx.finditer(target_str):
                    if len(re.findall(r"[A-Za-z0-9]+", alias)) == 1 and not _single_token_alias_is_standalone(target_str, m.start(), m.end()):
                        continue
                    raw_matches.append({
                        "entity": {"id": p_id, "name": p_name},
                        "matched_alias": alias.strip(),
                        "span": (m.start(), m.end()),
                        "is_name": False
                    })

    matched_performers = _filter_subsumed_matches(raw_matches)

    if len(matched_performers) == 0:
        return {"matched": False, "reason": "No matching performer found in filename"}

    # Check for genuine overlapping span collisions between different performers
    matches_list = list(matched_performers.values())
    for i in range(len(matches_list)):
        for j in range(i + 1, len(matches_list)):
            m1, m2 = matches_list[i], matches_list[j]
            s1, e1 = m1["span"]
            s2, e2 = m2["span"]
            if max(s1, s2) < min(e1, e2):
                return {
                    "matched": False,
                    "reason": f"Ambiguous performer match in filename ({m1['entity']['name']} vs {m2['entity']['name']})"
                }

    entities = [m["entity"] for m in matches_list]
    return {
        "matched": True,
        "entities": entities,
        "entity": entities[0],
        "matches": matches_list,
        "matched_alias": matches_list[0]["matched_alias"] if len(entities) == 1 else None,
        "source": "filename"
    }


def match_studio_for_filing(scene: dict, filename: str, all_studios: list[dict], match_source: str = "metadata_first") -> dict:
    """Conservatively match a studio for automatic filing.
    - Prefer complete canonical-name matches over shorter aliases contained within the same span.
    - Preserve ambiguity when genuinely separate studios match, or when conflicting identities match the same span."""
    if match_source in ("metadata_first", "metadata_only"):
        scene_studio = scene.get("studio")
        if scene_studio and scene_studio.get("id") and scene_studio.get("name"):
            return {
                "matched": True,
                "entity": {"id": str(scene_studio["id"]), "name": scene_studio["name"]},
                "matched_alias": None,
                "source": "metadata"
            }
        elif match_source == "metadata_only":
            return {"matched": False, "reason": "No studio tagged in scene metadata"}

    target_str = f"{Path(filename).stem} | {scene.get('title') or ''}".strip(" |")
    if not target_str:
        return {"matched": False, "reason": "No filename or title to match"}

    alias_owner = {}
    ambiguous_aliases = set()
    for s in all_studios:
        s_id = str(s.get("id"))
        aliases = s.get("aliases") or []
        for alias in aliases:
            a_norm = alias.strip().lower()
            if not a_norm:
                continue
            if a_norm in alias_owner and alias_owner[a_norm] != s_id:
                ambiguous_aliases.add(a_norm)
            else:
                alias_owner[a_norm] = s_id

    raw_matches = []
    for s in all_studios:
        s_id = str(s.get("id"))
        s_name = (s.get("name") or "").strip()
        if not s_name:
            continue

        name_norm = s_name.lower()
        if len(name_norm) >= 3 and name_norm not in NOISE_ALIASES:
            rx = _build_token_regex(s_name)
            if rx:
                for m in rx.finditer(target_str):
                    raw_matches.append({
                        "entity": {"id": s_id, "name": s_name},
                        "matched_alias": None,
                        "span": (m.start(), m.end()),
                        "is_name": True
                    })

        aliases = s.get("aliases") or []
        for alias in aliases:
            a_norm = alias.strip().lower()
            if len(a_norm) < 3 or a_norm in NOISE_ALIASES or a_norm in ambiguous_aliases:
                continue
            rx = _build_token_regex(alias)
            if rx:
                for m in rx.finditer(target_str):
                    if len(re.findall(r"[A-Za-z0-9]+", alias)) == 1 and not _single_token_alias_is_standalone(target_str, m.start(), m.end()):
                        continue
                    raw_matches.append({
                        "entity": {"id": s_id, "name": s_name},
                        "matched_alias": alias.strip(),
                        "span": (m.start(), m.end()),
                        "is_name": False
                    })

    matched_studios = _filter_subsumed_matches(raw_matches)

    if len(matched_studios) == 0:
        return {"matched": False, "reason": "No matching studio found in filename"}
    elif len(matched_studios) > 1:
        names = [m["entity"]["name"] for m in matched_studios.values()]
        return {"matched": False, "reason": f"Multiple studios matched in filename ({', '.join(names[:3])}); ambiguous destination"}
    else:
        match = list(matched_studios.values())[0]
        return {
            "matched": True,
            "entity": match["entity"],
            "matched_alias": match["matched_alias"],
            "source": "filename"
        }


def get_configured_filing_destination_roots(config: dict | None) -> list[str]:
    """Return a cleaned, deduplicated list of configured destination roots for automatic filing,
    excluding any roots configured as cold storage."""
    if not config or not isinstance(config, dict):
        return []
    explicit_raw = config.get("autoFilingDestinationRoots")
    has_explicit = bool(explicit_raw) or bool(config.get("autoFilingDestinationRoot"))
    override_setting = config.get("autoFilingDestinationRootsOverride")
    use_explicit = override_setting is True or (override_setting is None and has_explicit)
    raw = explicit_raw if use_explicit else config.get("_libraryRoots")
    roots = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, (str, Path)) and str(item).strip():
                roots.append(str(item).strip())
    elif isinstance(raw, str) and raw.strip():
        for chunk in raw.splitlines():
            for part in chunk.split(","):
                part_str = part.strip()
                if part_str:
                    roots.append(part_str)

    # Fallback to legacy single root setting if roots is empty
    if not roots and use_explicit:
        legacy = str(config.get("autoFilingDestinationRoot") or "").strip()
        if legacy:
            roots.append(legacy)

    # Filter out cold storage roots (never target cold storage drives for automatic filing)
    cold_roots = [
        os.path.normpath(str(c)).rstrip(os.sep)
        for c in parse_cold_storage_roots(config.get("coldStorageRoots"))
        if str(c).strip()
    ]

    # Deduplicate while preserving order and excluding cold storage roots
    seen = set()
    deduped = []
    for r in roots:
        norm = os.path.normpath(str(r)).rstrip(os.sep)
        if norm not in seen:
            seen.add(norm)
            if cold_roots and any(
                norm == c or norm.startswith(c + os.sep) or c.startswith(norm + os.sep)
                for c in cold_roots
            ):
                continue
            deduped.append(r)
    return deduped



def get_filing_folder_mappings(database_path: Path) -> list[dict]:
    """Return all custom entity-to-folder mappings."""
    connection = connect(database_path)
    try:
        rows = connection.execute(
            "SELECT * FROM filing_folder_mappings ORDER BY entity_type, entity_name COLLATE NOCASE"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        connection.close()


def save_filing_folder_mapping(
    database_path: Path,
    entity_type: str,
    entity_id: str,
    entity_name: str,
    folder_path: str,
    configured_roots: list[str] | None = None
) -> tuple[bool, str]:
    """Validate and save a custom folder mapping for a Stash performer, studio, or tag.
    Enforces:
    - Entity type must be 'performer', 'studio', or 'tag'.
    - Entity ID and name must be non-empty.
    - Folder path must exist on disk as a directory.
    - Folder path must be located inside one of configured destination roots (if roots provided).
    - Multiple entities can share the same destination folder, including cross-type mappings.
    - Never creates or renames folders automatically.
    """
    etype = (entity_type or "").strip().lower()
    if etype not in ("performer", "studio", "tag"):
        return False, f"Invalid entity type: '{entity_type}'. Must be 'performer', 'studio', or 'tag'."
    eid = str(entity_id or "").strip()
    if not eid:
        return False, "Entity ID is required."
    ename = str(entity_name or "").strip()
    if not ename:
        return False, "Entity name is required."
    fstr = str(folder_path or "").strip()
    if not fstr:
        return False, "Folder path is required."

    try:
        fpath = Path(fstr).expanduser().resolve()
    except Exception as exc:
        return False, f"Invalid folder path '{fstr}': {exc}"

    if not fpath.is_dir():
        return False, f"Mapped folder does not exist on disk: '{fstr}'. Watchtower never creates folders automatically."

    # Validate that folder is within one of the configured destination roots
    if configured_roots:
        is_inside_root = False
        resolved_folder_str = str(fpath)
        for root in configured_roots:
            if not root or not str(root).strip():
                continue
            try:
                root_res = str(Path(root).expanduser().resolve())
                if resolved_folder_str == root_res or resolved_folder_str.startswith(root_res + os.sep):
                    is_inside_root = True
                    break
            except Exception:
                continue
        if not is_inside_root:
            roots_str = ", ".join(f"'{r}'" for r in configured_roots if str(r).strip())
            return False, f"Mapped folder '{fstr}' is outside configured destination roots ({roots_str})."

    connection = connect(database_path)
    try:
        now = utc_now()
        existing = connection.execute(
            "SELECT id FROM filing_folder_mappings WHERE entity_type=? AND entity_id=?",
            (etype, eid)
        ).fetchone()

        if existing:
            connection.execute(
                "UPDATE filing_folder_mappings SET entity_name=?, folder_path=?, updated_at=? WHERE id=?",
                (ename, str(fpath), now, existing["id"])
            )
        else:
            connection.execute(
                """INSERT INTO filing_folder_mappings
                   (entity_type, entity_id, entity_name, folder_path, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (etype, eid, ename, str(fpath), now, now)
            )
        connection.commit()
        invalidate_destination_dir_cache()
        record_activity(
            database_path, "config", "folder mapping saved", "complete",
            detail=f"Saved custom folder mapping for {etype} '{ename}' -> {fpath.name}"
        )
        return True, "Mapping saved successfully."
    finally:
        connection.close()


def delete_filing_folder_mapping(database_path: Path, mapping_id: int) -> bool:
    """Delete a custom folder mapping."""
    connection = connect(database_path)
    try:
        row = connection.execute("SELECT * FROM filing_folder_mappings WHERE id=?", (mapping_id,)).fetchone()
        if not row:
            return False
        connection.execute("DELETE FROM filing_folder_mappings WHERE id=?", (mapping_id,))
        connection.commit()
        invalidate_destination_dir_cache()
        record_activity(
            database_path, "config", "folder mapping deleted", "complete",
            detail=f"Deleted custom folder mapping for {row['entity_type']} '{row['entity_name']}'"
        )
        return True
    finally:
        connection.close()


_INCOMING_DISCOVERY_CACHE: dict[str, tuple[float, list[dict]]] = {}
_INCOMING_DISCOVERY_CACHE_LOCK = threading.Lock()
_INCOMING_DISCOVERY_CACHE_TTL = 3600.0  # 1 hour; explicit recheck remains available


def invalidate_incoming_discovery_cache(root_path: str | Path | None = None) -> None:
    """Invalidate cached incoming directory scan results in memory."""
    with _INCOMING_DISCOVERY_CACHE_LOCK:
        if root_path is None:
            _INCOMING_DISCOVERY_CACHE.clear()
        else:
            try:
                norm_target = os.path.normcase(str(Path(root_path).expanduser())).replace(chr(92), "/")
                keys_to_remove = [k for k in _INCOMING_DISCOVERY_CACHE if norm_target in k]
                for k in keys_to_remove:
                    _INCOMING_DISCOVERY_CACHE.pop(k, None)
            except Exception:
                _INCOMING_DISCOVERY_CACHE.clear()


def discover_incoming_files_cached(
    incoming_folders: list[str],
    active_incoming_states: dict[str, str] | None = None,
    force_refresh: bool = False,
    ttl: float = _INCOMING_DISCOVERY_CACHE_TTL
) -> list[dict]:
    """Scan configured incoming folders for actionable completed files with a 1-hour cache.
    - Reuses cached discovery when roots match and within TTL.
    - Explicit force_refresh or TTL expiry rescans available incoming folders.
    - Filters out active incomplete lifecycle states and temporary download files.
    """
    if not incoming_folders:
        return []
    
    active_states = active_incoming_states or {}
    normalized_roots = sorted([
        os.path.normcase(str(Path(f).expanduser())).replace(chr(92), "/")
        for f in incoming_folders if f and str(f).strip()
    ])
    cache_key = json.dumps(normalized_roots)
    now_mono = time.monotonic()

    current_mtimes = {}
    for f in incoming_folders:
        try:
            folder = Path(f).expanduser()
            if folder.is_dir():
                current_mtimes[str(folder)] = folder.stat().st_mtime_ns
        except Exception:
            pass

    if not force_refresh:
        with _INCOMING_DISCOVERY_CACHE_LOCK:
            cached = _INCOMING_DISCOVERY_CACHE.get(cache_key)
            if cached:
                cached_mono, cached_mtimes, cached_entries = cached
                if now_mono - cached_mono < ttl and cached_mtimes == current_mtimes:
                    return [
                        dict(e) for e in cached_entries
                        if is_actionable_incoming_file(e["path"], active_states.get(e["path"]))
                    ]

    fresh_entries = []
    seen_paths = set()
    for incoming_folder in incoming_folders:
        folder = Path(incoming_folder).expanduser()
        if not folder.is_dir():
            continue
        try:
            for root, _dirs, filenames in os.walk(folder):
                for filename in filenames:
                    current_path = Path(root) / filename
                    current_path_str = str(current_path)
                    if current_path_str in seen_paths:
                        continue
                    if not is_actionable_incoming_file(current_path, active_states.get(current_path_str)):
                        continue
                    try:
                        stat = current_path.stat()
                    except OSError:
                        continue
                    seen_paths.add(current_path_str)
                    fresh_entries.append({
                        "path": current_path_str,
                        "size": stat.st_size,
                        "modified_ns": stat.st_mtime_ns,
                        "oshash": None,
                        "seen_at": utc_now(),
                    })
        except OSError:
            continue

    with _INCOMING_DISCOVERY_CACHE_LOCK:
        _INCOMING_DISCOVERY_CACHE[cache_key] = (now_mono, current_mtimes, fresh_entries)

    return [dict(e) for e in fresh_entries]


_DESTINATION_DIR_CACHE: dict[tuple[str, int], tuple[float, list[tuple[Path, str]]]] = {}
_DESTINATION_DIR_CACHE_LOCK = threading.Lock()
_DESTINATION_DIR_CACHE_TTL = 3600.0  # 1 hour; explicit folder refresh remains available
_CACHE_GENERATION: int = 0


def invalidate_destination_dir_cache(root_path: str | Path | None = None, database_path: Path | None = None) -> None:
    """Invalidate cached destination directories in memory and persistent database cache."""
    global _CACHE_GENERATION
    with _DESTINATION_DIR_CACHE_LOCK:
        _CACHE_GENERATION += 1
        if root_path is None:
            _DESTINATION_DIR_CACHE.clear()
        else:
            try:
                norm_key = str(Path(root_path).expanduser().resolve())
                keys_to_remove = [k for k in _DESTINATION_DIR_CACHE if k[0] == norm_key]
                for k in keys_to_remove:
                    _DESTINATION_DIR_CACHE.pop(k, None)
            except Exception:
                pass

    if database_path:
        try:
            conn = connect(database_path)
            try:
                if root_path is None:
                    conn.execute("DELETE FROM filing_destination_dir_cache")
                    conn.execute("DELETE FROM filing_destination_cache_meta")
                else:
                    norm_key = str(Path(root_path).expanduser().resolve())
                    conn.execute("DELETE FROM filing_destination_dir_cache WHERE root_path=?", (norm_key,))
                    conn.execute("DELETE FROM filing_destination_cache_meta WHERE root_path=?", (norm_key,))
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            logger.debug("Failed invalidating database destination cache: %s", exc)


def refresh_destination_dir_cache(
    database_path: Path,
    roots: list[str],
    max_depth: int = 4,
    cold_storage_roots: list[str] | set[str] | None = None
) -> dict:
    """Explicitly rescan destination roots on disk and store a fresh snapshot in SQLite database."""
    safe_depth = max(1, min(8, int(max_depth)))
    total_folders = 0
    scanned_roots = []

    if cold_storage_roots:
        cold_norms = [os.path.normpath(str(c)) for c in cold_storage_roots if str(c).strip()]
        roots = [
            r for r in roots
            if not any(
                os.path.normpath(str(r)) == c or
                os.path.normpath(str(r)).startswith(c + os.sep) or
                c.startswith(os.path.normpath(str(r)) + os.sep)
                for c in cold_norms
            )
        ]

    invalidate_destination_dir_cache(database_path=database_path)

    conn = connect(database_path)
    try:
        now_str = utc_now()
        now_mono = time.monotonic()
        for root_str in roots:
            if not root_str or not str(root_str).strip():
                continue
            try:
                root_path = Path(root_str).expanduser().resolve()
            except Exception:
                continue
            if not root_path.is_dir():
                continue

            root_key = str(root_path)
            scan_fn = _get_core_fn("_scan_destination_subdirectories", _scan_destination_subdirectories)
            entries = scan_fn(root_path, safe_depth)

            with _DESTINATION_DIR_CACHE_LOCK:
                _DESTINATION_DIR_CACHE[(root_key, safe_depth)] = (now_mono, entries)

            conn.execute("DELETE FROM filing_destination_dir_cache WHERE root_path=?", (root_key,))
            conn.execute("DELETE FROM filing_destination_cache_meta WHERE root_path=?", (root_key,))

            rows = [
                (root_key, str(p), norm, len(p.parts) - len(root_path.parts))
                for p, norm in entries
            ]
            if rows:
                conn.executemany(
                    "INSERT INTO filing_destination_dir_cache (root_path, dir_path, norm_name, depth) VALUES (?, ?, ?, ?)",
                    rows
                )
            conn.execute(
                "INSERT INTO filing_destination_cache_meta (root_path, max_depth, scanned_at, entry_count, generation) VALUES (?, ?, ?, ?, ?)",
                (root_key, safe_depth, now_str, len(entries), _CACHE_GENERATION)
            )
            conn.commit()
            total_folders += len(entries)
            scanned_roots.append(root_key)

        record_activity(
            database_path, "config", "folders refreshed", "complete",
            detail=f"Refreshed destination folders snapshot: found {total_folders} directories across {len(scanned_roots)} root(s)"
        )
        return {
            "success": True,
            "total_folders": total_folders,
            "scanned_roots": scanned_roots,
            "scanned_at": now_str
        }
    finally:
        conn.close()


_EXCLUDED_DESTINATION_DISCOVERY_NAMES = {
    ".ds_store", ".git", ".stfolder", ".stash", ".thumbnails",
    "@eadir", "lost+found", ".bin", ".trashes", ".temporaryitems",
    ".bin", ".recycle", "recycle.bin",
    "_orphaned_covers", "orphaned_covers",
    "deleted_mismatched_covers",
    "_recovery", "recovery",
    "_watchtower", "watchtower",
}


def _is_excluded_destination_discovery_dir(dirname: str) -> bool:
    d_lower = dirname.strip().lower()
    if not d_lower:
        return True
    if d_lower.startswith((".", "_orphaned", "orphaned_", "_recovery", "_watchtower", "deleted_mismatched", "$", "@")):
        return True
    if d_lower in _EXCLUDED_DESTINATION_DISCOVERY_NAMES:
        return True
    return False


def _scan_destination_subdirectories(
    root_path: Path,
    max_depth: int = 4,
    cold_storage_roots: list[str] | set[str] | None = None
) -> list[tuple[Path, str]]:
    """Recursively scan a destination root up to a bounded depth for existing subdirectories.
    Skips hidden/system directories, maintenance, recovery, and orphaned-cover directories.
    In-place filtering of dirnames ensures all descendants of excluded directories are skipped.
    Returns list of (resolved_path, normalized_name) tuples."""
    if cold_storage_roots:
        root_str = os.path.normpath(str(root_path))
        for c in cold_storage_roots:
            c_norm = os.path.normpath(str(c))
            if root_str == c_norm or root_str.startswith(c_norm + os.sep) or c_norm.startswith(root_str + os.sep):
                return []
    results: list[tuple[Path, str]] = []
    try:
        root_resolved = root_path.expanduser().resolve()
        if cold_storage_roots:
            root_res_str = os.path.normpath(str(root_resolved))
            for c in cold_storage_roots:
                c_norm = os.path.normpath(str(c))
                if root_res_str == c_norm or root_res_str.startswith(c_norm + os.sep) or c_norm.startswith(root_res_str + os.sep):
                    return []
        if not root_resolved.is_dir():
            return []
        base_depth = len(root_resolved.parts)

        for dirpath, dirnames, _ in os.walk(str(root_resolved), topdown=True, followlinks=False):
            curr_path = Path(dirpath)
            current_depth = len(curr_path.parts) - base_depth
            if current_depth >= max_depth:
                dirnames.clear()
                continue

            # Safely filter out excluded directories and all their descendants in-place
            dirnames[:] = [
                d for d in dirnames
                if not _is_excluded_destination_discovery_dir(d)
            ]

            for d in dirnames:
                p = curr_path / d
                norm_name = _normalize_name_for_folder_match(d)
                results.append((p.resolve(), norm_name))
    except Exception as exc:
        logger.debug("Failed scanning destination subdirectories in %s: %s", root_path, exc)

    return results


def _get_cached_destination_subdirectories(
    root_path: Path,
    max_depth: int = 4,
    ttl: float = _DESTINATION_DIR_CACHE_TTL,
    database_path: Path | None = None,
    cold_storage_roots: list[str] | set[str] | None = None
) -> list[tuple[Path, str]]:
    """Retrieve subdirectories for a root from memory cache or persistent SQLite cache,
    or scan and store if missing/expired."""
    if cold_storage_roots:
        root_str = os.path.normpath(str(root_path))
        for c in cold_storage_roots:
            c_norm = os.path.normpath(str(c))
            if root_str == c_norm or root_str.startswith(c_norm + os.sep) or c_norm.startswith(root_str + os.sep):
                return []
    safe_depth = max(1, min(8, int(max_depth)))
    try:
        root_resolved = root_path.expanduser().resolve()
        if cold_storage_roots:
            root_res_str = os.path.normpath(str(root_resolved))
            for c in cold_storage_roots:
                c_norm = os.path.normpath(str(c))
                if root_res_str == c_norm or root_res_str.startswith(c_norm + os.sep) or c_norm.startswith(root_res_str + os.sep):
                    return []
        if not root_resolved.is_dir():
            return []
        root_key = str(root_resolved)
    except Exception:
        scan_fn = _get_core_fn("_scan_destination_subdirectories", _scan_destination_subdirectories)
        if cold_storage_roots is not None:
            return scan_fn(root_path, safe_depth, cold_storage_roots=cold_storage_roots)
        return scan_fn(root_path, safe_depth)

    cache_key = (root_key, safe_depth)
    now_mono = time.monotonic()

    # 1. Fast in-memory check
    with _DESTINATION_DIR_CACHE_LOCK:
        cached = _DESTINATION_DIR_CACHE.get(cache_key)
        if cached is not None:
            cached_time, entries = cached
            if now_mono - cached_time < ttl:
                return entries

    # 2. Check persistent SQLite cache across process boundaries
    if database_path:
        try:
            conn = connect(database_path)
            try:
                meta = conn.execute(
                    "SELECT max_depth, scanned_at, entry_count, generation FROM filing_destination_cache_meta WHERE root_path=?",
                    (root_key,)
                ).fetchone()
                if meta and meta["max_depth"] >= safe_depth and meta["generation"] >= _CACHE_GENERATION:
                    try:
                        scanned_dt = datetime.fromisoformat(meta["scanned_at"])
                        age_sec = (datetime.now(timezone.utc) - scanned_dt).total_seconds()
                    except Exception:
                        age_sec = 0
                    if age_sec < ttl:
                        rows = conn.execute(
                            "SELECT dir_path, norm_name FROM filing_destination_dir_cache WHERE root_path=? AND depth <= ?",
                            (root_key, safe_depth)
                        ).fetchall()
                        entries = [(Path(r["dir_path"]), r["norm_name"]) for r in rows]
                        with _DESTINATION_DIR_CACHE_LOCK:
                            _DESTINATION_DIR_CACHE[cache_key] = (now_mono, entries)
                        return entries
            finally:
                conn.close()
        except Exception as exc:
            logger.debug("Error reading SQLite destination cache: %s", exc)

    # 3. Cache miss or expired: scan destination root on disk
    scan_fn = _get_core_fn("_scan_destination_subdirectories", _scan_destination_subdirectories)
    if cold_storage_roots is not None:
        fresh_entries = scan_fn(root_resolved, safe_depth, cold_storage_roots=cold_storage_roots)
    else:
        fresh_entries = scan_fn(root_resolved, safe_depth)
    with _DESTINATION_DIR_CACHE_LOCK:
        _DESTINATION_DIR_CACHE[cache_key] = (now_mono, fresh_entries)

    # Populate SQLite persistent cache
    if database_path:
        try:
            conn = connect(database_path)
            try:
                now_str = utc_now()
                conn.execute("DELETE FROM filing_destination_dir_cache WHERE root_path=?", (root_key,))
                conn.execute("DELETE FROM filing_destination_cache_meta WHERE root_path=?", (root_key,))
                rows = [
                    (root_key, str(p), norm, len(p.parts) - len(root_resolved.parts))
                    for p, norm in fresh_entries
                ]
                if rows:
                    conn.executemany(
                        "INSERT INTO filing_destination_dir_cache (root_path, dir_path, norm_name, depth) VALUES (?, ?, ?, ?)",
                        rows
                    )
                conn.execute(
                    "INSERT INTO filing_destination_cache_meta (root_path, max_depth, scanned_at, entry_count, generation) VALUES (?, ?, ?, ?, ?)",
                    (root_key, safe_depth, now_str, len(fresh_entries), _CACHE_GENERATION)
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            logger.debug("Error updating SQLite destination cache: %s", exc)

    return fresh_entries


def resolve_filing_destinations(
    destination_roots: list[str],
    entity_name: str,
    entity_id: str | None = None,
    entity_type: str | None = None,
    database_path: Path | None = None,
    max_depth: int = 4
) -> tuple[list[Path], str]:
    """Find eligible existing destination subfolders across all configured destination roots.
    1. First checks for a custom folder mapping in database if provided.
       - Validates mapped folder exists on disk and is inside one of destination_roots (at any depth).
       - If valid, returns ([mapped_folder], 'custom_mapping').
    2. Otherwise scans all configured destination roots for nested child directories matching entity_name (cached).
    Returns:
    - ([], 'no_destination_roots') if no roots configured
    - ([], 'no_match') if 0 folders match
    - ([path], 'ok') if exactly 1 folder matches
    - ([path1, path2, ...], 'multiple_destinations') if >1 folders match
    """
    if not destination_roots:
        return [], "Destination root folder is not configured"

    # 1. Custom folder mapping check (supports any depth under destination roots).
    # Older UI builds could store the entity name in entity_id when their Stash
    # lookup failed. Prefer the stable ID, then accept an exact case-insensitive
    # name match so those existing mappings remain useful.
    if database_path and entity_id and entity_type:
        connection = connect(database_path)
        try:
            rows = connection.execute(
                "SELECT folder_path FROM filing_folder_mappings WHERE entity_type=? AND entity_id=?",
                (entity_type.lower(), str(entity_id))
            ).fetchall()
            if not rows and entity_name:
                rows = connection.execute(
                    """SELECT folder_path FROM filing_folder_mappings
                       WHERE entity_type=? AND entity_name=? COLLATE NOCASE""",
                    (entity_type.lower(), str(entity_name).strip())
                ).fetchall()
            valid_mapped: list[Path] = []
            seen_mapped: set[str] = set()
            for row in rows:
                folder_raw = str(row["folder_path"] or "").strip()
                if not folder_raw:
                    continue
                # Fast check without disk access: must be inside one of destination_roots
                if not any(_is_subpath_of(folder_raw, r) for r in destination_roots):
                    continue
                mapped_path = Path(folder_raw).expanduser()
                if mapped_path.is_dir():
                    res_mapped = str(mapped_path.resolve())
                    for r in destination_roots:
                        if not r:
                            continue
                        try:
                            r_res = str(Path(r).expanduser().resolve())
                            if res_mapped == r_res or res_mapped.startswith(r_res + os.sep):
                                if res_mapped not in seen_mapped:
                                    seen_mapped.add(res_mapped)
                                    valid_mapped.append(mapped_path.resolve())
                                break
                        except Exception:
                            continue
            if valid_mapped:
                return valid_mapped, "custom_mapping" if len(valid_mapped) == 1 else "multiple_destinations"
        finally:
            connection.close()

    # 2. Search all configured destination roots (bounded recursive discovery with cache)
    target_norm = _normalize_name_for_folder_match(entity_name)
    if not target_norm:
        return [], "Entity name is empty"
    target_compact = _compact_name_for_folder_match(target_norm)

    matched_folders = []
    seen_paths = set()
    for root_str in destination_roots:
        if not root_str or not str(root_str).strip():
            continue
        try:
            root_path = Path(root_str).expanduser().resolve()
        except Exception:
            continue
        if not root_path.is_dir():
            continue

        entries = _get_cached_destination_subdirectories(root_path, max_depth=max_depth, database_path=database_path)
        for dir_path, norm_name in entries:
            # Check exact normalized match or conservative descriptor match (e.g. 'The Cole Bentley Collection')
            stripped = _strip_conservative_folder_descriptors(norm_name)
            raw_stripped = norm_name
            if raw_stripped.startswith("the "):
                raw_stripped = raw_stripped[4:].strip()
            if raw_stripped.endswith(" collection"):
                raw_stripped = raw_stripped[:-11].strip()

            compact_match = (
                len(target_compact) >= 4 and
                (_compact_name_for_folder_match(norm_name) == target_compact or
                 _compact_name_for_folder_match(raw_stripped) == target_compact or
                 _compact_name_for_folder_match(stripped) == target_compact)
            )
            if norm_name == target_norm or raw_stripped == target_norm or stripped == target_norm or compact_match:
                res_str = str(dir_path)
                if res_str not in seen_paths:
                    seen_paths.add(res_str)
                    matched_folders.append(dir_path)

    if len(matched_folders) == 0:
        return [], f"Destination folder for '{entity_name}' does not exist under configured destination roots"
    elif len(matched_folders) == 1:
        return matched_folders, "ok"
    else:
        return matched_folders, "multiple_destinations"


_TAG_FOLDER_DESCRIPTOR_TOKENS = {
    "category", "categories", "collection", "collections", "content", "contents",
    "hair", "haired", "scene", "scenes", "tag", "tags", "video", "videos",
}


def _singular_category_token(token: str) -> str:
    token = token.lower()
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 4 and token.endswith("es") and token[-3:-2] in {"s", "x", "z"}:
        return token[:-2]
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _category_name_tokens(name: str, drop_descriptors: bool = False) -> set[str]:
    tokens = {
        _singular_category_token(token)
        for token in re.findall(r"[A-Za-z0-9]+", str(name or "").lower())
        if len(token) >= 3
    }
    if drop_descriptors:
        meaningful = tokens - _TAG_FOLDER_DESCRIPTOR_TOKENS
        if meaningful:
            return meaningful
    return tokens


def resolve_tag_filing_destinations(
    destination_roots: list[str],
    tag: dict,
    database_path: Path | None = None,
    max_depth: int = 4,
) -> tuple[list[Path], str]:
    """Resolve a verified Stash tag to existing category folders.

    Exact/custom mappings win. The fallback compares whole normalized word
    tokens, handles ordinary plurals, and ignores generic category descriptors.
    It never creates a folder and preserves multiple matches for user review.
    """
    tag_id = str(tag.get("id") or "").strip()
    canonical_name = str(tag.get("name") or "").strip()
    aliases = [str(alias).strip() for alias in (tag.get("aliases") or []) if str(alias).strip()]
    names = [name for name in [canonical_name, *aliases] if name]
    if not names:
        return [], "Tag name is empty"

    exact_matches: list[Path] = []
    seen = set()
    for name in names:
        paths, status = resolve_filing_destinations(
            destination_roots, name, entity_id=tag_id, entity_type="tag",
            database_path=database_path, max_depth=max_depth,
        )
        if status == "custom_mapping":
            return paths, status
        for path in paths:
            resolved = str(path.resolve())
            if resolved not in seen:
                seen.add(resolved)
                exact_matches.append(path)
    if exact_matches:
        return exact_matches, "ok" if len(exact_matches) == 1 else "multiple_destinations"

    tag_token_sets = [tokens for tokens in (_category_name_tokens(name, drop_descriptors=True) for name in names) if tokens]
    matched: list[Path] = []
    for root_str in destination_roots:
        if not root_str or not str(root_str).strip():
            continue
        try:
            root = Path(root_str).expanduser().resolve()
        except Exception:
            continue
        if not root.is_dir():
            continue
        for folder, folder_name in _get_cached_destination_subdirectories(
            root, max_depth=max_depth, database_path=database_path
        ):
            folder_tokens = _category_name_tokens(folder_name)
            if not folder_tokens:
                continue
            if any(tokens.issubset(folder_tokens) for tokens in tag_token_sets):
                resolved = str(folder.resolve())
                if resolved not in seen:
                    seen.add(resolved)
                    matched.append(folder)

    if not matched:
        return [], f"Destination folder for tag '{canonical_name}' does not exist under configured destination roots"
    return matched, "ok" if len(matched) == 1 else "multiple_destinations"


def resolve_filing_destination_folder(destination_root: str, entity_name: str) -> tuple[Path | None, str]:
    """Legacy single-root compatibility wrapper for resolve_filing_destinations."""
    paths, status = resolve_filing_destinations([destination_root] if destination_root else [], entity_name)
    if status in ("ok", "custom_mapping") and len(paths) == 1:
        return paths[0], "ok"
    elif status == "multiple_destinations":
        match_names = [m.name for m in paths]
        return None, f"Ambiguous destination: multiple folders match '{entity_name}' ({', '.join(match_names)})"
    else:
        return None, status


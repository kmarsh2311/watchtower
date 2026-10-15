"""Stash GraphQL client integration for Watchtower.

Encapsulates GraphQL queries, mutations, request dispatching, and reusable
operations interacting with the Stash server instance.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union

logger = logging.getLogger("librarymanager.integrations.stash_graphql")

# ---------------------------------------------------------------------------
# GraphQL Query & Mutation Constants
# ---------------------------------------------------------------------------

INVENTORY_SCENES_QUERY = """
query LibraryManagerInventory($filter: FindFilterType) {
  findScenes(filter: $filter) {
    count
    scenes {
      id title details date director code rating100 organized urls
      studio { name }
      performers { id name }
      tags { id name }
      galleries { id title }
      stash_ids { endpoint stash_id }
      groups { group { id name } scene_index }
      files { id path basename size duration width height video_codec fingerprints { type value } }
    }
  }
}
"""

# Alias for backwards compatibility with librarymanager.py
QUERY = INVENTORY_SCENES_QUERY

SCENE_QUERY = """
query LibraryManagerScene($id: ID!) {
  findScene(id: $id) {
    id title details date director code rating100 organized urls
    studio { name }
    performers { id name }
    tags { id name }
    galleries { id title }
    stash_ids { endpoint stash_id }
    groups { group { id name } scene_index }
    files { id path basename size duration width height video_codec fingerprints { type value } }
  }
}
"""

PERFORMER_SCENES_QUERY = """
query LibraryManagerPerformerScenes($id: ID!) {
  findScenes(scene_filter: { performers: { value: [$id], modifier: INCLUDES } }, filter: { per_page: -1 }) {
    scenes { id }
  }
}
"""

STUDIO_SCENES_QUERY = """
query LibraryManagerStudioScenes($id: ID!) {
  findScenes(scene_filter: { studios: { value: [$id], modifier: INCLUDES } }, filter: { per_page: -1 }) {
    scenes { id }
  }
}
"""

ROOTS_QUERY = "query LibraryManagerRoots { configuration { general { stashes { path } } } }"

SCENE_COUNT_QUERY = "query LibraryManagerSceneCount { findScenes(filter: {per_page: 1}) { count } }"

DELETE_FILES_MUTATION = """
mutation LibraryManagerDeleteFiles($ids: [ID!]!) {
  deleteFiles(ids: $ids)
}
"""

CHECK_SCENES_QUERY = """
query CheckScenes($ids: [ID!]) {
  findScenes(scene_filter: { ids: { value: $ids, modifier: INCLUDES } }, filter: { per_page: -1 }) {
    scenes { id files { id path } }
  }
}
"""

FIND_SCENE_BY_PATH_QUERY = """
query FindSceneByPath($path: String!) {
  findScenes(scene_filter: {path: {value: $path, modifier: EQUALS}}) {
    scenes {
      id title details date director code rating100 organized urls
      studio { name }
      performers { id name }
      tags { id name aliases }
      galleries { id title }
      stash_ids { endpoint stash_id }
      groups { group { id name } scene_index }
      files { id path basename size duration fingerprints { type value } }
    }
  }
}
"""

FIND_SCENE_BY_BASENAME_QUERY = """
query FindSceneByBasename($name: String!) {
  findScenes(scene_filter: {path: {value: $name, modifier: INCLUDES}}, filter: {per_page: 20}) {
    scenes {
      id title details date director code rating100 organized urls
      studio { name }
      performers { id name }
      tags { id name aliases }
      galleries { id title }
      stash_ids { endpoint stash_id }
      groups { group { id name } scene_index }
      files { id path basename size duration fingerprints { type value } }
    }
  }
}
"""

FIND_MISSING_SCENES_QUERY = """
query FindMissingScenes {
  findScenes(filter: {per_page: 500}) {
    scenes {
      id
      title
      files { id path basename size fingerprints { type value } }
    }
  }
}
"""

FIND_SCENE_FOR_FILING_QUERY = """
query FindSceneForFiling($id: ID!) {
  findScene(id: $id) {
    id title
    files { id path }
    performers { id name disambiguation alias_list }
    studio { id name aliases }
    tags { id name aliases }
  }
}
"""

CHECK_SCENE_FILES_QUERY = """
query CheckScene($id: ID!) {
  findScene(id: $id) {
    id
    files { id path }
  }
}
"""

CHECK_SCENE_EXISTS_QUERY = """
query CheckSceneExists($id: ID!) {
  findScene(id: $id) {
    id
  }
}
"""

ALL_STUDIOS_QUERY = "{ allStudios { id name aliases } }"

ALL_PERFORMERS_QUERY = "{ allPerformers { id name disambiguation alias_list } }"

FIND_FILES_QUERY = "query { findFiles(filter: {per_page: -1}) { files { path } } }"

CONFIGURATION_VIDEO_EXTENSIONS_QUERY = "query { configuration { general { videoExtensions } } }"

CONFIGURE_GENERAL_MUTATION = """
mutation ConfigureGeneral($input: ConfigGeneralInput!) {
  configureGeneral(input: $input) {
    videoExtensions
  }
}
"""

METADATA_GENERATE_MUTATION = """
mutation GenerateForScene($input: GenerateMetadataInput!) {
  metadataGenerate(input: $input)
}
"""

SCENE_UPDATE_MUTATION = """
mutation SceneUpdate($input: SceneUpdateInput!) {
  sceneUpdate(input: $input) {
    id
  }
}
"""

JOB_QUEUE_QUERY = "{ jobQueue { id description status progress } }"

UI_CONFIGURATION_QUERY = "{ configuration { ui } }"

API_KEY_QUERY = "query getApiKey { configuration { general { apiKey } } }"


# ---------------------------------------------------------------------------
# Core GraphQL Dispatcher
# ---------------------------------------------------------------------------

def execute_gql(
    stash: Any,
    query: str,
    variables: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Safely executes a GraphQL query or mutation against Stash.

    Args:
        stash: StashInterface instance or mock with call_GQL.
        query: GraphQL query or mutation document.
        variables: Optional variables dictionary.

    Returns:
        Data dictionary from Stash, or None if stash is not available or query fails.
    """
    if not stash:
        return None
    if not hasattr(stash, "call_GQL"):
        return None
    try:
        if variables is not None:
            return stash.call_GQL(query, variables)
        return stash.call_GQL(query)
    except Exception as exc:
        logger.debug("GraphQL execution failed for query '%s...': %s", query[:60].strip(), exc)
        return None


# ---------------------------------------------------------------------------
# Reusable Operations
# ---------------------------------------------------------------------------

def fetch_scenes(stash: Any, page_size: int = 250) -> List[Dict[str, Any]]:
    """Fetches all scenes from Stash using paginated GraphQL queries."""
    scenes: List[Dict[str, Any]] = []
    page = 1
    while True:
        result = execute_gql(stash, INVENTORY_SCENES_QUERY, {"filter": {"page": page, "per_page": page_size}})
        found = (result or {}).get("findScenes") or {}
        batch = found.get("scenes") or []
        scenes.extend(batch)
        if not batch or len(scenes) >= int(found.get("count") or 0):
            return scenes
        page += 1


def fetch_scene(stash: Any, scene_id: Union[str, int]) -> Optional[Dict[str, Any]]:
    """Fetches a single scene by ID with full fields."""
    result = execute_gql(stash, SCENE_QUERY, {"id": str(scene_id)})
    return (result or {}).get("findScene")


def current_scene_count(stash: Any) -> int:
    """Returns total count of scenes present in Stash."""
    result = execute_gql(stash, SCENE_COUNT_QUERY)
    return int(((result or {}).get("findScenes") or {}).get("count") or 0)


def fetch_library_roots(stash: Any) -> List[str]:
    """Fetches sorted unique library root paths from Stash configuration."""
    result = execute_gql(stash, ROOTS_QUERY)
    stashes = (((result or {}).get("configuration") or {}).get("general") or {}).get("stashes") or []
    return sorted({str(item.get("path")).strip() for item in stashes if item.get("path")})


def fetch_all_studios(stash: Any) -> List[Dict[str, Any]]:
    """Fetches all studios (id, name, aliases) from Stash."""
    res = execute_gql(stash, ALL_STUDIOS_QUERY)
    return (res or {}).get("allStudios") or []


def fetch_all_performers(stash: Any) -> List[Dict[str, Any]]:
    """Fetches all performers (id, name, disambiguation, alias_list) from Stash."""
    res = execute_gql(stash, ALL_PERFORMERS_QUERY)
    return (res or {}).get("allPerformers") or []


def fetch_all_files(stash: Any) -> List[Dict[str, Any]]:
    """Fetches all file entries from Stash."""
    res = execute_gql(stash, FIND_FILES_QUERY)
    return ((res or {}).get("findFiles") or {}).get("files") or []


def find_scene_by_path(stash: Any, path: str) -> Optional[Dict[str, Any]]:
    """Finds a scene in Stash matching the given path, falling back to basename."""
    if not stash or not path:
        return None

    # 1. Exact path query
    res = execute_gql(stash, FIND_SCENE_BY_PATH_QUERY, {"path": path})
    scenes = ((res or {}).get("findScenes") or {}).get("scenes") or []
    if scenes:
        return scenes[0]

    # 2. Fallback to basename search and match normalized paths
    bn = Path(path).name
    res = execute_gql(stash, FIND_SCENE_BY_BASENAME_QUERY, {"name": bn})
    candidates = ((res or {}).get("findScenes") or {}).get("scenes") or []
    norm_path = os.path.normcase(os.path.abspath(path))
    for candidate_sc in candidates:
        for f in candidate_sc.get("files") or []:
            fpath = str(f.get("path") or "")
            if fpath == path or os.path.normcase(os.path.abspath(fpath)) == norm_path:
                return candidate_sc
    return None


def check_scenes_batch(
    stash: Any,
    scene_ids: Sequence[Union[str, int]],
    batch_size: int = 100,
) -> Tuple[Set[str], Set[str], bool]:
    """Queries Stash for a batch of scene IDs and returns (active_scene_ids, active_file_ids, query_succeeded)."""
    active_scenes: Set[str] = set()
    active_files: Set[str] = set()
    if not stash or not scene_ids:
        return active_scenes, active_files, False

    query_succeeded = False
    id_list = [str(sid) for sid in scene_ids]
    for i in range(0, len(id_list), batch_size):
        chunk = id_list[i : i + batch_size]
        subqueries = [f's{idx}: findScene(id: "{sid}") {{ id files {{ id path }} }}' for idx, sid in enumerate(chunk)]
        query = "query CheckScenes { " + " ".join(subqueries) + " }"
        res = execute_gql(stash, query)
        if isinstance(res, dict):
            query_succeeded = True
            if "findScenes" in res:
                scenes_list = (res.get("findScenes") or {}).get("scenes") or []
            else:
                scenes_list = [val for val in res.values() if isinstance(val, dict) and "id" in val]
            for sc in scenes_list:
                active_scenes.add(str(sc.get("id")))
                for f in sc.get("files") or []:
                    fid = f.get("id")
                    if fid is not None:
                        active_files.add(str(fid))
    return active_scenes, active_files, query_succeeded


def check_scene_files(stash: Any, scene_id: Union[str, int]) -> Optional[Dict[str, Any]]:
    """Queries a single scene's file records from Stash."""
    res = execute_gql(stash, CHECK_SCENE_FILES_QUERY, {"id": str(scene_id)})
    return (res or {}).get("findScene")


def check_scene_exists(stash: Any, scene_id: Union[str, int]) -> bool:
    """Checks whether a scene exists in Stash."""
    res = execute_gql(stash, CHECK_SCENE_EXISTS_QUERY, {"id": str(scene_id)})
    return bool((res or {}).get("findScene"))


def delete_files(stash: Any, file_ids: Sequence[Union[str, int]]) -> bool:
    """Deletes files from Stash by file IDs."""
    if not stash or not file_ids:
        return False
    ids = [str(fid) for fid in file_ids]
    res = execute_gql(stash, DELETE_FILES_MUTATION, {"ids": ids})
    return bool(res and res.get("deleteFiles"))


def ensure_stash_supports_extension(stash: Any, path: str) -> None:
    """Ensures Stash's configured videoExtensions include this file's extension."""
    if not stash or not path:
        return
    ext = Path(path).suffix.lstrip(".").lower()
    if not ext:
        return
    try:
        cfg = execute_gql(stash, CONFIGURATION_VIDEO_EXTENSIONS_QUERY)
        current_exts = set(((cfg or {}).get("configuration") or {}).get("general", {}).get("videoExtensions") or [])
        if ext not in current_exts:
            current_exts.add(ext)
            execute_gql(
                stash,
                CONFIGURE_GENERAL_MUTATION,
                {"input": {"videoExtensions": sorted(list(current_exts))}},
            )
            logger.info("Added supported extension '%s' to Stash videoExtensions", ext)
    except Exception as ext_err:
        logger.debug("Failed checking/updating Stash videoExtensions: %s", ext_err)


def update_scene_performers(stash: Any, scene_id: Union[str, int], performer_ids: List[str]) -> bool:
    """Updates the performer IDs assigned to a scene."""
    res = execute_gql(
        stash,
        SCENE_UPDATE_MUTATION,
        {"input": {"id": str(scene_id), "performer_ids": [str(pid) for pid in performer_ids]}},
    )
    return bool(res and res.get("sceneUpdate"))


def update_scene_studio(stash: Any, scene_id: Union[str, int], studio_id: str) -> bool:
    """Sets the studio ID for a scene."""
    res = execute_gql(
        stash,
        SCENE_UPDATE_MUTATION,
        {"input": {"id": str(scene_id), "studio_id": str(studio_id)}},
    )
    return bool(res and res.get("sceneUpdate"))


def update_scene_tags(stash: Any, scene_id: Union[str, int], tag_ids: List[str]) -> bool:
    """Sets the tag IDs for a scene."""
    res = execute_gql(
        stash,
        SCENE_UPDATE_MUTATION,
        {"input": {"id": str(scene_id), "tag_ids": [str(tid) for tid in tag_ids]}},
    )
    return bool(res and res.get("sceneUpdate"))

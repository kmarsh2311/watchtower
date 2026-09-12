#!/usr/bin/env python3
"""Small launchd entry point that starts the Library Manager watcher when Stash is available."""

import argparse
import json
from pathlib import Path

from stashapi.stashapp import StashInterface

from librarymanager import start_filesystem_monitor


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", required=True)
    args = parser.parse_args()
    runtime = json.loads(Path(args.runtime).read_text(encoding="utf-8"))
    stash = StashInterface(runtime.get("server_connection") or {})
    config = stash.find_plugin_config("librarymanager") or {}
    if config.get("autoStartMonitor") is True:
        start_filesystem_monitor(stash, Path(runtime["database"]), runtime.get("server_connection") or {})


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # launchd retries every minute. Stash may simply not be running yet.
        pass

"""Runtime dependency bootstrap for Watchtower.

Ensures required packages (stashapp-tools / stashapi) are available in the
running Python interpreter environment, automatically bootstrapping them if missing.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def ensure_dependencies(target_dir: Path | str | None = None) -> None:
    """Ensure stashapi is importable, attempting automated installation if missing."""
    try:
        import stashapi  # noqa: F401
        from stashapi.stashapp import StashInterface  # noqa: F401
        return
    except ModuleNotFoundError:
        pass

    # Log to stderr so Stash records the bootstrap attempt
    print("[Watchtower] Initialising required dependency 'stashapp-tools'...", file=sys.stderr, flush=True)

    dest_dir = Path(target_dir).resolve() if target_dir else Path(__file__).resolve().parent
    python_exe = sys.executable or "python"

    # Attempt progressive installation strategies:
    # 1. Standard pip install into the active interpreter environment
    # 2. User-level install (--user) if permissions on system site-packages are restricted
    # 3. Direct target install into destination directory (always writable by Stash)
    install_commands = [
        [python_exe, "-m", "pip", "install", "stashapp-tools", "--quiet"],
        [python_exe, "-m", "pip", "install", "stashapp-tools", "--user", "--quiet"],
        [python_exe, "-m", "pip", "install", "stashapp-tools", "--target", str(dest_dir), "--quiet"],
    ]

    last_error = None
    for cmd in install_commands:
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
            if res.returncode == 0:
                if str(dest_dir) not in sys.path:
                    sys.path.insert(0, str(dest_dir))
                try:
                    import stashapi  # noqa: F401
                    from stashapi.stashapp import StashInterface  # noqa: F401
                    print("[Watchtower] 'stashapp-tools' successfully installed.", file=sys.stderr, flush=True)
                    return
                except ModuleNotFoundError:
                    continue
            else:
                last_error = (res.stderr or res.stdout).strip()
        except Exception as err:
            last_error = str(err)

    raise RuntimeError(
        f"Watchtower requires 'stashapp-tools', which could not be automatically installed "
        f"using '{python_exe}'. Last error: {last_error}. "
        f"Please run '{python_exe} -m pip install stashapp-tools' in your terminal."
    )

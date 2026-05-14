#!/usr/bin/env python3
"""Cursor afterFileEdit hook: stdin JSON with file_path; regen viewer if it is a diagram source."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from generate_diagram_viewer import GENERATOR_RELPATH, match_diagram_source


def main() -> int:
    raw = sys.stdin.read()
    if not raw.strip():
        return 0
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        print("cursor_mermaid_after_file_edit: invalid JSON on stdin", file=sys.stderr)
        return 0

    path_str = payload.get("file_path")
    if not path_str or not isinstance(path_str, str):
        return 0

    repo = match_diagram_source(Path(path_str))
    if repo is None:
        return 0

    proc = subprocess.run(
        [sys.executable, str(repo / GENERATOR_RELPATH)],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.stdout:
        sys.stdout.write(proc.stdout)
    if proc.returncode != 0:
        print(
            f"cursor_mermaid_after_file_edit: generator exited {proc.returncode}",
            file=sys.stderr,
        )
        if proc.stderr:
            sys.stderr.write(proc.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

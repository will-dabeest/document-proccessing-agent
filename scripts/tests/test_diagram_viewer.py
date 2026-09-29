"""Regression tests for the Mermaid diagram viewer generator and edit hook.

The generator writes HTML that is opened in a browser. Titles are escaped;
diagram bodies are inserted raw, so a body containing a closing tag or a
script tag must abort before the file is replaced.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import cursor_mermaid_after_file_edit as hook
import generate_diagram_viewer as viewer

_SOURCES = (
    "docs/architecture.md",
    "docs/cursor-agent-workflow.md",
)


def _write_sources(root: Path, architecture: str, workflow: str = "## Empty\n") -> None:
    docs = root / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "architecture.md").write_text(architecture, encoding="utf-8")
    (docs / "cursor-agent-workflow.md").write_text(workflow, encoding="utf-8")


def _patch_root(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setattr(viewer, "REPO_ROOT", root)
    monkeypatch.setattr(viewer, "SOURCE_MARKDOWN", _SOURCES)


def test_extract_uses_nearest_heading_and_ignores_other_fences(tmp_path: Path) -> None:
    md = tmp_path / "doc.md"
    md.write_text(
        "#### Not a section\n"
        "## System context\n"
        "```python\n"
        "mermaid = 1\n"
        "```\n"
        "### Request flow\n"
        "```mermaid extra\n"
        "flowchart LR\n"
        "  A-->B\n"
        "```\n"
        "## Next\n"
        "```Mermaid\n"
        "graph TD\n"
        "```\n"
        "```mermaid\n"
        "## inside the diagram\n"
        "graph LR\n"
        "```\n"
        "```mermaid\n"
        "sequenceDiagram\n"
        "```\n",
        encoding="utf-8",
    )

    assert viewer.extract_diagrams(md, "docs/architecture.md") == [
        ("Request flow", "flowchart LR\n  A-->B"),
        ("Next", "## inside the diagram\ngraph LR"),
        ("Next", "sequenceDiagram"),
    ]


def test_h2_clears_stale_h3_and_unclosed_fence_is_dropped(tmp_path: Path) -> None:
    md = tmp_path / "doc.md"
    md.write_text(
        "## One\n"
        "### Sub\n"
        "```mermaid\n"
        "graph A\n"
        "```\n"
        "## Two\n"
        "```mermaid\n"
        "graph B\n",
        encoding="utf-8",
    )

    assert viewer.extract_diagrams(md, "docs/x.md") == [("Sub", "graph A")]


def test_fallback_title_when_no_heading(tmp_path: Path) -> None:
    md = tmp_path / "doc.md"
    md.write_text("```mermaid\ngraph TD\n```\n", encoding="utf-8")

    assert viewer.extract_diagrams(md, "docs/architecture.md") == [
        ("docs/architecture.md — diagram 1", "graph TD")
    ]


def test_repo_docs_keep_every_mermaid_block() -> None:
    root = Path(__file__).resolve().parents[2]
    for rel in viewer.SOURCE_MARKDOWN:
        path = root / rel
        expected = path.read_text(encoding="utf-8").count("```mermaid")
        diagrams = viewer.extract_diagrams(path, rel)
        assert len(diagrams) == expected
        assert expected > 0
        assert all(title.strip() and body.strip() for title, body in diagrams)


def test_main_escapes_heading_html(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _write_sources(
        tmp_path,
        "## Ops & <script>alert(1)</script>\n```mermaid\nflowchart LR\n  A-->B\n```\n",
    )
    _patch_root(monkeypatch, tmp_path)

    assert viewer.main() == 0

    html = (tmp_path / "docs" / "diagrams-viewer.html").read_text(encoding="utf-8")
    assert "<h2>Ops &amp; &lt;script&gt;alert(1)&lt;/script&gt;</h2>" in html
    assert "<h2>Ops & <script>" not in html
    assert "flowchart LR\n  A-->B" in html
    captured = capsys.readouterr()
    assert "1 diagram(s)" in captured.out


@pytest.mark.parametrize(
    "body",
    [
        "flowchart LR\nA --> B</div><script>alert(1)</script>",
        "flowchart LR\nA --> B\n<ScRiPt>alert(1)</ScRiPt>",
    ],
)
def test_main_refuses_html_breakout_and_leaves_output_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    body: str,
) -> None:
    _write_sources(tmp_path, f"## Safe title\n```mermaid\n{body}\n```\n")
    _patch_root(monkeypatch, tmp_path)
    out = tmp_path / "docs" / "diagrams-viewer.html"
    out.write_text("UNCHANGED", encoding="utf-8")

    assert viewer.main() == 1

    assert out.read_text(encoding="utf-8") == "UNCHANGED"
    err = capsys.readouterr().err
    assert "forbidden HTML-like sequence" in err
    assert "docs/architecture.md" in err


def test_main_missing_source_does_not_write_viewer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_root(monkeypatch, tmp_path)

    assert viewer.main() == 1

    assert not (tmp_path / "docs" / "diagrams-viewer.html").exists()
    assert "missing source" in capsys.readouterr().err


def test_find_repo_root_requires_generator_script(tmp_path: Path) -> None:
    assert viewer.find_repo_root(tmp_path) is None
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "generate_diagram_viewer.py").write_text("# stub\n", encoding="utf-8")
    nested = tmp_path / "docs" / "nested"
    nested.mkdir(parents=True)
    assert viewer.find_repo_root(nested) == tmp_path


def test_match_diagram_source_only_known_markdown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "generate_diagram_viewer.py").write_text("# stub\n", encoding="utf-8")
    arch = tmp_path / "docs" / "architecture.md"
    arch.parent.mkdir()
    arch.write_text("# x\n", encoding="utf-8")
    readme = tmp_path / "README.md"
    readme.write_text("no\n", encoding="utf-8")

    assert viewer.match_diagram_source(arch) == tmp_path
    assert viewer.match_diagram_source(readme) is None

    monkeypatch.setattr(viewer, "find_repo_root", lambda _start: tmp_path)
    assert viewer.match_diagram_source(Path("/etc/passwd")) is None


def _run_hook(monkeypatch: pytest.MonkeyPatch, payload: str) -> int:
    monkeypatch.setattr(hook.sys, "stdin", io.StringIO(payload))
    return hook.main()


def test_hook_ignores_empty_invalid_and_non_source_payloads(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ran = MagicMock()
    monkeypatch.setattr(hook.subprocess, "run", ran)

    assert _run_hook(monkeypatch, "   \n") == 0
    assert _run_hook(monkeypatch, "{") == 0
    assert "invalid JSON" in capsys.readouterr().err
    assert _run_hook(monkeypatch, json.dumps({"file_path": 12})) == 0
    assert _run_hook(monkeypatch, json.dumps({})) == 0

    monkeypatch.setattr(hook, "match_diagram_source", lambda _path: None)
    assert _run_hook(monkeypatch, json.dumps({"file_path": "/tmp/notes.md"})) == 0
    ran.assert_not_called()


def test_hook_regenerates_diagram_source_and_swallows_generator_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = Path("/repo")
    monkeypatch.setattr(hook, "match_diagram_source", lambda _path: repo)
    proc = MagicMock(returncode=1, stdout="partial\n", stderr="error: missing source\n")
    monkeypatch.setattr(hook.subprocess, "run", MagicMock(return_value=proc))

    assert _run_hook(
        monkeypatch, json.dumps({"file_path": str(repo / "docs" / "architecture.md")})
    ) == 0

    hook.subprocess.run.assert_called_once_with(
        [sys.executable, str(repo / viewer.GENERATOR_RELPATH)],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    captured = capsys.readouterr()
    assert captured.out == "partial\n"
    assert "generator exited 1" in captured.err
    assert "error: missing source" in captured.err

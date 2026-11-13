from __future__ import annotations

from pathlib import Path

from scripts.verify_markdown import check


def test_markdown_checker_accepts_local_links_and_generated_heading_anchors(tmp_path: Path) -> None:
    (tmp_path / "guide.md").write_text(
        "# Setup Guide\n\n## Setup Guide\n\n## Setup & Details\n\n## Greek Θ\n",
        encoding="utf-8",
    )
    (tmp_path / "index.md").write_text(
        "[guide](guide.md#setup-guide-1) [ampersand](guide.md#setup--details) "
        "[Greek](guide.md#greek-Θ) and [same](#top)\n\n# Top\n",
        encoding="utf-8",
    )
    assert check(tmp_path) == []


def test_markdown_checker_reports_missing_paths_and_anchors_with_line_numbers(tmp_path: Path) -> None:
    (tmp_path / "index.md").write_text("[missing](nope.md)\n[anchor](#gone)\n", encoding="utf-8")
    assert check(tmp_path) == [
        "index.md:1: missing target: nope.md",
        "index.md:2: missing anchor: #gone",
    ]


def test_markdown_checker_ignores_external_links_and_fenced_examples(tmp_path: Path) -> None:
    (tmp_path / "index.md").write_text(
        "[external](https://example.com/not-local)\n\n```md\n[example](missing.md)\n```\n",
        encoding="utf-8",
    )
    assert check(tmp_path) == []


def test_markdown_checker_rejects_links_that_escape_workspace(tmp_path: Path) -> None:
    (tmp_path / "index.md").write_text("[outside](../outside.md)\n", encoding="utf-8")
    assert check(tmp_path) == ["index.md:1: link escapes workspace: ../outside.md"]


def test_markdown_checker_accepts_setext_heading_anchors(tmp_path: Path) -> None:
    (tmp_path / "index.md").write_text(
        "[section](#installation-guide) [phase](#phase-2)\n\n"
        "Installation Guide\n==================\n\n# Phase 2\n",
        encoding="utf-8",
    )
    assert check(tmp_path) == []


def test_markdown_checker_checks_uppercase_markdown_extensions(tmp_path: Path) -> None:
    (tmp_path / "Guide.MD").write_text("# Setup\n", encoding="utf-8")
    (tmp_path / "index.md").write_text("[guide](Guide.MD#missing)\n", encoding="utf-8")
    assert check(tmp_path) == ["index.md:1: missing anchor: Guide.MD#missing"]

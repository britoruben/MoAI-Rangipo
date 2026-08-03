# -*- coding: utf-8 -*-
"""Offline tests for build.py — no network, no API."""

import json
import os
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import build


class TestCleanMarkdown(unittest.TestCase):
    def test_strips_headings(self):
        result = build.clean_markdown("# Title\n\nSome text.")
        self.assertNotIn("#", result)
        self.assertIn("Title", result)
        self.assertIn("Some text", result)

    def test_strips_code_fences(self):
        result = build.clean_markdown("Before\n```python\ncode here\n```\nAfter")
        self.assertNotIn("code here", result)
        self.assertIn("Before", result)
        self.assertIn("After", result)

    def test_strips_wikilinks_keeps_text(self):
        result = build.clean_markdown("See [[some-note]] for details.")
        self.assertIn("some-note", result)
        self.assertNotIn("[[", result)
        self.assertNotIn("]]", result)

    def test_strips_markdown_links_keeps_text(self):
        result = build.clean_markdown("See [click here](http://example.com) now.")
        self.assertIn("click here", result)
        self.assertNotIn("http://example.com", result)

    def test_collapses_whitespace(self):
        result = build.clean_markdown("a   b\n\n\nc")
        self.assertNotIn("  ", result)


class _TempDocs:
    """Context manager: creates a temp DOCS_DIR with optional files."""
    def __init__(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = self._td.name
        self.ideas_dir = os.path.join(self.root, "ideas")
        self.projects_dir = os.path.join(self.root, "projects")
        os.makedirs(self.ideas_dir, exist_ok=True)
        os.makedirs(self.projects_dir, exist_ok=True)

    def write(self, rel_path, content):
        abs_path = os.path.join(self.root, rel_path)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(content)

    def patch(self):
        # ROOT must also be patched: collect_md_nodes uses os.path.relpath(path, ROOT),
        # which fails on Windows if the temp dir is on a different drive than the project.
        return unittest.mock.patch.multiple(
            build,
            ROOT=self.root,
            DOCS_DIR=self.root,
            DOCS_IDEAS_DIR=self.ideas_dir,
            DOCS_PROJECTS_DIR=self.projects_dir,
            DOCS_SKIP=set(),
        )

    def __enter__(self):
        self._td.__enter__()
        return self

    def __exit__(self, *a):
        return self._td.__exit__(*a)


class TestCollectMdNodes(unittest.TestCase):
    def test_note_idea_project_types(self):
        with _TempDocs() as td:
            td.write("note.md",             "# Note One\nSome content.")
            td.write("captures/cap.md",     "# Capture\nVoice capture.")
            td.write("ideas/my-idea.md",    "# My Idea\nAn idea.")
            td.write("projects/proj.md",    "# My Project\nA project.")
            with td.patch():
                entries = build.collect_md_nodes()
        by_type = {e["type"] for e in entries}
        self.assertEqual(len(entries), 4)
        self.assertIn("note",    by_type)
        self.assertIn("idea",    by_type)
        self.assertIn("project", by_type)

    def test_skips_non_md_files(self):
        with _TempDocs() as td:
            td.write("readme.txt",  "not markdown")
            td.write("image.png",   "")
            td.write("note.md",     "# Note\nContent.")
            with td.patch():
                entries = build.collect_md_nodes()
        self.assertEqual(len(entries), 1)

    def test_respects_docs_skip(self):
        with _TempDocs() as td:
            td.write("user-note.md",      "# User Note\nContent.")
            td.write("en/arch.md",        "# Architecture\nDocs.")
            td.write("es/plan.md",        "# Plan\nSpanish docs.")
            # re-apply real DOCS_SKIP so en/ and es/ are excluded
            with unittest.mock.patch.multiple(
                build,
                ROOT=td.root,
                DOCS_DIR=td.root,
                DOCS_IDEAS_DIR=td.ideas_dir,
                DOCS_PROJECTS_DIR=td.projects_dir,
                DOCS_SKIP={"en", "es"},
            ):
                entries = build.collect_md_nodes()
        self.assertEqual(len(entries), 1)
        self.assertIn("user-note", entries[0]["stem"])


class TestBuildOutput(unittest.TestCase):
    def _run_build(self, td):
        out_file = os.path.join(td.root, "graph-data.js")
        with td.patch(), \
             unittest.mock.patch.object(build, "OUT_FILE", out_file), \
             unittest.mock.patch.object(build, "CONNECTORS_FILE", "/nonexistent"), \
             unittest.mock.patch.object(build, "TOOLS_FILE", "/nonexistent"):
            build.build()
        with open(out_file, encoding="utf-8") as f:
            raw = f.read()
        start = raw.index("const GRAPH =") + len("const GRAPH =")
        return json.loads(raw[start:].strip().rstrip(";").strip())

    def test_id_equals_index(self):
        with _TempDocs() as td:
            td.write("a.md", "# Alpha\nContent.")
            td.write("b.md", "# Beta\nContent.")
            td.write("c.md", "# Gamma\nContent.")
            graph = self._run_build(td)
        for i, node in enumerate(graph["nodes"]):
            self.assertEqual(node["id"], i, "id mismatch at index %d" % i)

    def test_links_not_duplicated(self):
        with _TempDocs() as td:
            td.write("alpha.md", "# Alpha\nSee [[beta]] for details.")
            td.write("beta.md",  "# Beta\nAlpha is a great note.")
            graph = self._run_build(td)
        seen = set()
        for link in graph["links"]:
            key = (min(link["source"], link["target"]),
                   max(link["source"], link["target"]))
            self.assertNotIn(key, seen, "Duplicate link: %s" % str(key))
            seen.add(key)

    def test_valid_json_output(self):
        with _TempDocs() as td:
            td.write("note.md", "# Note\nSome content.")
            graph = self._run_build(td)
        self.assertIn("nodes", graph)
        self.assertIn("links", graph)
        self.assertIn("mtime", graph)


if __name__ == "__main__":
    unittest.main()

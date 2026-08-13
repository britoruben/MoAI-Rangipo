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


class TestMdLabel(unittest.TestCase):
    def test_uses_first_heading(self):
        self.assertEqual(build.md_label("# Real Title\ntext", "some-stem"), "Real Title")

    def test_ignores_headings_inside_code_fences(self):
        raw = "```\n# Not A Heading\n```\nbody"
        self.assertEqual(build.md_label(raw, "my-note"), "My note")

    def test_falls_back_to_humanized_stem(self):
        self.assertEqual(build.md_label("no heading here", "my-cool-note"), "My cool note")


class TestCollectMdNodesEdgeCases(unittest.TestCase):
    def test_missing_docs_dir_returns_empty(self):
        with unittest.mock.patch.object(build, "DOCS_DIR", "/nonexistent-docs-dir"):
            self.assertEqual(build.collect_md_nodes(), [])


class TestCollectJsonNodes(unittest.TestCase):
    def _write(self, td, name, content):
        path = os.path.join(td.root, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    def test_missing_file_returns_empty(self):
        self.assertEqual(
            build.collect_json_nodes("/nonexistent.json", "connectors", "connector"), []
        )

    def test_invalid_json_returns_empty(self):
        with _TempDocs() as td:
            path = self._write(td, "connectors.json", "{not json")
            self.assertEqual(build.collect_json_nodes(path, "connectors", "connector"), [])

    def test_non_dict_root_returns_empty(self):
        with _TempDocs() as td:
            path = self._write(td, "connectors.json", "[1, 2, 3]")
            self.assertEqual(build.collect_json_nodes(path, "connectors", "connector"), [])

    def test_non_list_key_returns_empty(self):
        with _TempDocs() as td:
            path = self._write(td, "tools.json", json.dumps({"tools": {"a": 1}}))
            self.assertEqual(build.collect_json_nodes(path, "tools", "tool"), [])

    def test_non_dict_items_are_skipped(self):
        with _TempDocs() as td:
            payload = {"tools": ["oops", {"id": "tts", "label": "TTS", "status": "active"}]}
            path = self._write(td, "tools.json", json.dumps(payload))
            entries = build.collect_json_nodes(path, "tools", "tool")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["label"], "TTS")
        self.assertEqual(entries[0]["type"], "tool")
        self.assertEqual(entries[0]["path"], "tools.json#tts")

    def test_label_falls_back_to_id(self):
        with _TempDocs() as td:
            payload = {"connectors": [{"id": "notion"}]}
            path = self._write(td, "connectors.json", json.dumps(payload))
            entries = build.collect_json_nodes(path, "connectors", "connector")
        self.assertEqual(entries[0]["label"], "notion")


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

    def test_long_excerpt_is_truncated_on_a_word_boundary(self):
        with _TempDocs() as td:
            body = " ".join(["word%d" % i for i in range(400)])
            td.write("long.md", "# Long\n%s" % body)
            graph = self._run_build(td)
        excerpt = graph["nodes"][0]["excerpt"]
        self.assertTrue(excerpt.endswith("…"))
        self.assertLessEqual(len(excerpt), build.EXTRACT_LEN + 1)
        self.assertNotIn(" …", excerpt)

    def test_capabilities_link_to_the_hub_project(self):
        with _TempDocs() as td:
            hub_rel = "projects/example-project-moai-galaxy.md"
            td.write(hub_rel, "# MoAI Galaxy\nThe central project.")
            connectors = os.path.join(td.root, "connectors.json")
            tools = os.path.join(td.root, "tools.json")
            with open(connectors, "w", encoding="utf-8") as f:
                json.dump({"connectors": [{"id": "notion", "label": "Notion"}]}, f)
            with open(tools, "w", encoding="utf-8") as f:
                json.dump({"tools": [{"id": "tts", "label": "Speech"}]}, f)
            out_file = os.path.join(td.root, "graph-data.js")
            with td.patch(), \
                 unittest.mock.patch.object(build, "OUT_FILE", out_file), \
                 unittest.mock.patch.object(build, "CONNECTORS_FILE", connectors), \
                 unittest.mock.patch.object(build, "TOOLS_FILE", tools), \
                 unittest.mock.patch.object(build, "HUB_PATH", hub_rel):
                build.build()
            with open(out_file, encoding="utf-8") as f:
                raw = f.read()
        start = raw.index("const GRAPH =") + len("const GRAPH =")
        graph = json.loads(raw[start:].strip().rstrip(";").strip())
        by_path = {n["path"]: n["id"] for n in graph["nodes"]}
        hub_id = by_path[hub_rel]
        linked = set()
        for link in graph["links"]:
            if link["source"] == hub_id:
                linked.add(link["target"])
            elif link["target"] == hub_id:
                linked.add(link["source"])
        self.assertIn(by_path["connectors.json#notion"], linked)
        self.assertIn(by_path["tools.json#tts"], linked)


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""Shared test fixture: a throwaway galaxy on disk.

Lives outside the test modules so every test file (helpers, API client,
HTTP handler) can build its own isolated repo without duplicating the
path patching.
"""

import json
import os
import sys
import tempfile
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import build
import server


class TempGalaxy:
    """Context manager: a temp repo (docs/, connectors.json, tools.json,
    graph-data.js) with server.py and build.py's module-level path
    constants patched to point at it. Used to test the CRUD helpers
    (find_notes, delete_note_by_title, manage_entity) against real files
    without touching the actual project data."""

    def __init__(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = self._td.name
        self.docs_dir = os.path.join(self.root, "docs")
        self.captures_dir = os.path.join(self.docs_dir, "captures")
        self.ideas_dir = os.path.join(self.docs_dir, "ideas")
        self.projects_dir = os.path.join(self.docs_dir, "projects")
        for d in (self.docs_dir, self.captures_dir, self.ideas_dir, self.projects_dir):
            os.makedirs(d, exist_ok=True)
        self.connectors_file = os.path.join(self.root, "connectors.json")
        self.tools_file = os.path.join(self.root, "tools.json")
        self.graph_file = os.path.join(self.root, "graph-data.js")
        self.config_file = os.path.join(self.root, "config.json")
        self.config_el_file = os.path.join(self.root, "config-el.json")
        self.preferences_file = os.path.join(self.root, "preferences.json")
        with open(self.connectors_file, "w", encoding="utf-8") as f:
            json.dump({"connectors": []}, f)
        with open(self.tools_file, "w", encoding="utf-8") as f:
            json.dump({"tools": []}, f)

    def write_note(self, rel_path, content):
        abs_path = os.path.join(self.docs_dir, rel_path)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(content)

    def write_json(self, path, data):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)

    def read_note(self, rel_path):
        with open(os.path.join(self.docs_dir, rel_path), encoding="utf-8") as f:
            return f.read()

    def patch(self):
        build_patch = unittest.mock.patch.multiple(
            build,
            ROOT=self.root,
            DOCS_DIR=self.docs_dir,
            DOCS_IDEAS_DIR=self.ideas_dir,
            DOCS_PROJECTS_DIR=self.projects_dir,
            CONNECTORS_FILE=self.connectors_file,
            TOOLS_FILE=self.tools_file,
            OUT_FILE=self.graph_file,
        )
        server_patch = unittest.mock.patch.multiple(
            server,
            ROOT=self.root,
            DOCS_DIR=self.docs_dir,
            CAPTURES_DIR=self.captures_dir,
            GRAPH_FILE=self.graph_file,
            CONNECTORS_FILE=self.connectors_file,
            TOOLS_FILE=self.tools_file,
            CONFIG_FILE=self.config_file,
            CONFIG_EL_FILE=self.config_el_file,
            PREFERENCES_FILE=self.preferences_file,
        )
        return build_patch, server_patch

    def __enter__(self):
        self._td.__enter__()
        self._patches = self.patch()
        for p in self._patches:
            p.__enter__()
        # each test starts against its own file, on a fresh graph cache
        server._graph_cache["stamp"] = None
        server._graph_cache["graph"] = None
        build.build()
        return self

    def __exit__(self, *a):
        for p in reversed(self._patches):
            p.__exit__(*a)
        return self._td.__exit__(*a)

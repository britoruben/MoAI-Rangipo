# -*- coding: utf-8 -*-
"""Offline tests for server.py's helpers: path guards, config loading,
note writing, the graph cache, and the Dev-mode operation whitelist.
No network, no API calls."""

import contextlib
import json
import os
import sys
import tempfile
import threading
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import build
import server
from tests.galaxy import TempGalaxy


class TestSafeEditablePath(unittest.TestCase):
    def test_accepts_markdown_under_docs(self):
        with TempGalaxy() as g:
            resolved = server._safe_editable_path("docs/captures/note.md")
        self.assertEqual(resolved, os.path.join(g.captures_dir, "note.md"))

    def test_rejects_empty_path(self):
        with TempGalaxy():
            with self.assertRaises(server._RequestError) as ctx:
                server._safe_editable_path("")
        self.assertEqual(ctx.exception.status, 400)

    def test_rejects_parent_traversal(self):
        with TempGalaxy():
            with self.assertRaises(server._RequestError) as ctx:
                server._safe_editable_path("docs/../../etc/passwd.md")
        self.assertEqual(ctx.exception.status, 400)

    def test_rejects_path_outside_docs(self):
        with TempGalaxy():
            with self.assertRaises(server._RequestError) as ctx:
                server._safe_editable_path("connectors.json.md")
        self.assertEqual(ctx.exception.status, 403)

    def test_rejects_project_docs_as_read_only(self):
        with TempGalaxy():
            for rel in ("docs/en/ARCHITECTURE.md", "docs/es/ARQUITECTURA.md"):
                with self.assertRaises(server._RequestError) as ctx:
                    server._safe_editable_path(rel)
                self.assertEqual(ctx.exception.status, 403)

    def test_rejects_non_markdown(self):
        with TempGalaxy():
            with self.assertRaises(server._RequestError) as ctx:
                server._safe_editable_path("docs/captures/note.txt")
        self.assertEqual(ctx.exception.status, 400)


class TestUpsertLastEdited(unittest.TestCase):
    def test_replaces_existing_line(self):
        content = "# Title\n\n*Last edited: 2020-01-01.*\n\nBody.\n"
        result = server._upsert_last_edited(content, "2024-05-06")
        self.assertIn("*Last edited: 2024-05-06.*", result)
        self.assertNotIn("2020-01-01", result)
        self.assertEqual(result.count("*Last edited:"), 1)

    def test_inserts_after_created_line(self):
        content = "# Title\n\n*Created: 2024-01-01.*\n\nBody.\n"
        result = server._upsert_last_edited(content, "2024-05-06")
        created_at = result.index("*Created:")
        edited_at = result.index("*Last edited:")
        self.assertLess(created_at, edited_at)
        self.assertLess(edited_at, result.index("Body."))

    def test_inserts_after_heading_when_no_created_line(self):
        result = server._upsert_last_edited("# Title\n\nBody.\n", "2024-05-06")
        self.assertLess(result.index("# Title"), result.index("*Last edited:"))

    def test_prepends_when_no_heading(self):
        result = server._upsert_last_edited("Just a body.", "2024-05-06")
        self.assertTrue(result.startswith("*Last edited: 2024-05-06.*"))


class TestRuntimeMode(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, server, "RUNTIME_MODE", server.RUNTIME_MODE)

    def test_production_enables_external_ai_and_web_search(self):
        status = server.set_runtime_mode("production")
        self.assertEqual(status["mode"], "production")
        self.assertTrue(status["external_ai"])
        self.assertTrue(status["web_search"])

    def test_dev_disables_external_ai_and_web_search(self):
        with unittest.mock.patch.object(server, "dev_mode_allowed", return_value=True):
            status = server.set_runtime_mode("dev")
        self.assertEqual(status["mode"], "dev")
        self.assertFalse(status["external_ai"])
        self.assertFalse(status["web_search"])
        self.assertTrue(status["local_tools"])

    def test_unknown_mode_raises_400(self):
        with self.assertRaises(server._RequestError) as ctx:
            server.set_runtime_mode("staging")
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(server.runtime_status()["mode"], "production")


class TestSessionLock(unittest.TestCase):
    def test_same_session_gets_the_same_lock(self):
        lock = server.session_lock("session-a")
        self.addCleanup(server._session_locks.pop, "session-a", None)
        self.addCleanup(server._session_locks.pop, "session-b", None)
        self.assertIs(lock, server.session_lock("session-a"))
        self.assertIsInstance(lock, type(threading.Lock()))

    def test_different_sessions_get_different_locks(self):
        self.addCleanup(server._session_locks.pop, "session-a", None)
        self.addCleanup(server._session_locks.pop, "session-b", None)
        self.assertIsNot(server.session_lock("session-a"), server.session_lock("session-b"))


class TestLoadConfig(unittest.TestCase):
    def test_reads_json_config(self):
        with TempGalaxy() as g:
            g.write_json(g.config_file, {"api_key": "k", "model": "claude-x"})
            self.assertEqual(server.load_config()["model"], "claude-x")

    def test_missing_file_raises(self):
        with TempGalaxy():
            with self.assertRaises(server._RequestError) as ctx:
                server.load_config()
        self.assertEqual(ctx.exception.status, 500)


class TestLoadPreferencesShape(unittest.TestCase):
    def test_non_dict_root_returns_sentinel(self):
        with TempGalaxy() as g:
            g.write_json(g.preferences_file, ["es", "Alex"])
            self.assertEqual(server.load_preferences(), {"lang": None, "name": None})

    def test_unknown_lang_and_blank_name_are_dropped(self):
        with TempGalaxy() as g:
            g.write_json(g.preferences_file, {"lang": "fr", "name": "   "})
            self.assertEqual(server.load_preferences(), {"lang": None, "name": None})


class TestLoadElevenlabsConfig(unittest.TestCase):
    def test_missing_file_returns_empty(self):
        with TempGalaxy():
            self.assertEqual(server.load_elevenlabs_config(), {})

    def test_invalid_json_returns_empty(self):
        with TempGalaxy() as g:
            with open(g.config_el_file, "w", encoding="utf-8") as f:
                f.write("{not json")
            self.assertEqual(server.load_elevenlabs_config(), {})

    def test_incomplete_config_returns_empty(self):
        with TempGalaxy() as g:
            g.write_json(g.config_el_file, {"api_key": "k"})
            self.assertEqual(server.load_elevenlabs_config(), {})

    def test_complete_config_returned_as_is(self):
        with TempGalaxy() as g:
            g.write_json(g.config_el_file, {"api_key": "k", "voice_id": "v"})
            self.assertEqual(server.load_elevenlabs_config(),
                             {"api_key": "k", "voice_id": "v"})


class TestLoadJsonList(unittest.TestCase):
    def test_missing_file_returns_empty(self):
        self.assertEqual(server.load_json_list("/nonexistent.json", "tools"), [])

    def test_invalid_json_returns_empty(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "tools.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{{{")
            self.assertEqual(server.load_json_list(path, "tools"), [])

    def test_list_root_returns_empty(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "tools.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump([{"id": "a"}], f)
            self.assertEqual(server.load_json_list(path, "tools"), [])

    def test_non_list_key_returns_empty(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "tools.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"tools": {"id": "a"}}, f)
            self.assertEqual(server.load_json_list(path, "tools"), [])

    def test_returns_items(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "tools.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"tools": [{"id": "web-search"}]}, f)
            self.assertEqual(server.load_json_list(path, "tools"), [{"id": "web-search"}])


class TestEntityFileHelpers(unittest.TestCase):
    def test_missing_file_returns_empty_dict(self):
        self.assertEqual(server._load_entity_file("/nonexistent.json"), {})

    def test_invalid_json_returns_empty_dict(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "x.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("nope")
            self.assertEqual(server._load_entity_file(path), {})

    def test_list_root_returns_empty_dict(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "x.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump([1, 2], f)
            self.assertEqual(server._load_entity_file(path), {})

    def test_save_then_load_round_trips(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "x.json")
            server._save_entity_file(path, {"tools": [{"id": "a", "label": "á"}]})
            self.assertEqual(server._load_entity_file(path),
                             {"tools": [{"id": "a", "label": "á"}]})

    def test_save_leaves_no_temp_file_behind(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "x.json")
            server._save_entity_file(path, {"tools": []})
            self.assertEqual(os.listdir(td), ["x.json"])


class TestLoadGraph(unittest.TestCase):
    def test_caches_until_the_file_changes(self):
        with TempGalaxy() as g:
            g.write_note("alpha.md", "# Alpha\nContent.")
            build.build()
            first = server.load_graph()
            self.assertIs(first, server.load_graph(), "unchanged file should hit the cache")

            g.write_note("beta.md", "# Beta\nContent.")
            build.build()
            second = server.load_graph()
        self.assertEqual({n["label"] for n in second["nodes"]}, {"Alpha", "Beta"})

    def test_rebuild_inside_one_timestamp_tick_is_not_stale(self):
        with TempGalaxy() as g:
            g.write_note("alpha.md", "# Alpha\nContent.")
            build.build()
            server.load_graph()
            os.remove(os.path.join(g.docs_dir, "alpha.md"))
            build.build()
            # same mtime as the previous build on coarse-timestamp filesystems
            os.utime(g.graph_file, ns=(0, 0))
            self.assertEqual(server.load_graph()["nodes"], [])


class TestWriteCapture(unittest.TestCase):
    def test_strips_spanish_trigger_phrase(self):
        with TempGalaxy():
            rel, title, abs_path = server.write_capture("recuerda que el moai vigila")
            self.assertTrue(os.path.isfile(abs_path))
        self.assertTrue(rel.startswith("docs/captures/"))
        self.assertEqual(title, "El moai vigila")

    def test_strips_english_trigger_phrase(self):
        with TempGalaxy():
            _rel, title, _abs = server.write_capture("remember that the moai watches")
        self.assertEqual(title, "The moai watches")

    def test_bare_trigger_raises(self):
        with TempGalaxy():
            with self.assertRaises(RuntimeError):
                server.write_capture("recuerda que")

    def test_writes_heading_and_created_date(self):
        with TempGalaxy():
            rel, title, abs_path = server.write_capture("el volcán despierta")
            with open(abs_path, encoding="utf-8") as f:
                content = f.read()
        self.assertTrue(content.startswith("# %s" % title))
        self.assertIn("*Created:", content)
        self.assertTrue(rel.endswith("el-volcan-despierta.md"))

    def test_duplicate_slug_gets_a_suffix(self):
        with TempGalaxy():
            first, _t, _a = server.write_capture("el volcán despierta")
            second, _t, _a = server.write_capture("el volcán despierta")
        self.assertEqual(first, "docs/captures/el-volcan-despierta.md")
        self.assertEqual(second, "docs/captures/el-volcan-despierta-2.md")


class TestRemember(unittest.TestCase):
    def test_returns_new_node_id_and_title(self):
        with TempGalaxy():
            result = server.remember("el volcán Rano Raraku despierta")
        node = result["graph"]["nodes"][result["new_id"]]
        self.assertEqual(node["label"], result["title"])
        self.assertEqual(node["type"], "note")

    def test_related_id_is_a_linked_neighbour(self):
        with TempGalaxy() as g:
            g.write_note("volcano.md", "# Volcano\nAbout the volcano.")
            build.build()
            result = server.remember("the volcano is awake")
        self.assertIsNotNone(result["related_id"])
        self.assertNotEqual(result["related_id"], result["new_id"])
        self.assertEqual(result["graph"]["nodes"][result["related_id"]]["label"], "Volcano")

    def test_related_id_none_in_an_empty_galaxy(self):
        with TempGalaxy():
            result = server.remember("un pensamiento aislado")
        self.assertIsNone(result["related_id"])

    def test_failed_build_removes_the_orphan_note(self):
        with TempGalaxy() as g:
            with unittest.mock.patch.object(build, "build", side_effect=OSError("disk full")):
                with self.assertRaises(RuntimeError):
                    server.remember("una nota que no cuaja")
            self.assertEqual(os.listdir(g.captures_dir), [])


class TestUpdateNoteByPath(unittest.TestCase):
    def test_writes_content_and_stamps_last_edited(self):
        with TempGalaxy() as g:
            g.write_note("alpha.md", "# Alpha\nOld.")
            build.build()
            result = server.update_note_by_path("docs/alpha.md", "# Alpha\nNew.\n")
            content = g.read_note("alpha.md")
        self.assertIsNotNone(result["node_id"])
        self.assertIn("New.", content)
        self.assertIn("*Last edited:", content)

    def test_missing_path_raises(self):
        with TempGalaxy():
            with self.assertRaises(RuntimeError):
                server.update_note_by_path("", "content")

    def test_unknown_note_raises(self):
        with TempGalaxy():
            with self.assertRaises(RuntimeError):
                server.update_note_by_path("docs/ghost.md", "content")


class TestDeleteNoteByPath(unittest.TestCase):
    def test_removes_file_and_node(self):
        with TempGalaxy() as g:
            g.write_note("alpha.md", "# Alpha\nContent.")
            build.build()
            result = server.delete_note_by_path("docs/alpha.md")
        self.assertEqual(result["path"], "docs/alpha.md")
        self.assertEqual(result["graph"]["nodes"], [])
        self.assertFalse(os.path.exists(os.path.join(g.docs_dir, "alpha.md")))

    def test_missing_path_raises(self):
        with TempGalaxy():
            with self.assertRaises(RuntimeError):
                server.delete_note_by_path("   ")

    def test_unknown_note_raises(self):
        with TempGalaxy():
            with self.assertRaises(RuntimeError):
                server.delete_note_by_path("docs/ghost.md")


class TestExecuteDevOperation(unittest.TestCase):
    def _dev(self):
        # Dev mode requires both RUNTIME_MODE == "dev" AND dev_mode_allowed()
        # (MOAI_DEV=1/--dev) — the second gate can't be armed over HTTP, so
        # tests exercising dev-mode behavior itself must patch it directly.
        stack = contextlib.ExitStack()
        stack.enter_context(unittest.mock.patch.object(server, "RUNTIME_MODE", "dev"))
        stack.enter_context(unittest.mock.patch.object(server, "dev_mode_allowed", return_value=True))
        return stack

    def test_notes_read_returns_content(self):
        with TempGalaxy() as g:
            g.write_note("alpha.md", "# Alpha\nBody.")
            build.build()
            with self._dev():
                result = server.execute_dev_operation("notes.read", {"path": "docs/alpha.md"})
        self.assertIn("Body.", result["content"])

    def test_notes_read_unknown_path_raises(self):
        with TempGalaxy():
            with self._dev():
                with self.assertRaises(RuntimeError):
                    server.execute_dev_operation("notes.read", {"path": "docs/ghost.md"})

    def test_notes_create_writes_a_capture(self):
        with TempGalaxy() as g:
            with self._dev():
                result = server.execute_dev_operation("notes.create", {"text": "una idea nueva"})
            self.assertIsNotNone(result["new_id"])
            self.assertEqual(len(os.listdir(g.captures_dir)), 1)

    def test_entity_list_and_crud(self):
        with TempGalaxy():
            with self._dev():
                self.assertEqual(server.execute_dev_operation("tools.list", {})["items"], [])
                created = server.execute_dev_operation(
                    "tools.create", {"id": "web-search", "label": "Web search"}
                )
                self.assertEqual([i["id"] for i in created["items"]], ["web-search"])
                updated = server.execute_dev_operation(
                    "tools.update", {"id": "web-search", "status": "placeholder"}
                )
                self.assertEqual(updated["items"][0]["status"], "placeholder")
                deleted = server.execute_dev_operation("tools.delete", {"id": "web-search"})
                self.assertEqual(deleted["items"], [])

    def test_graph_rebuild_returns_fresh_graph(self):
        with TempGalaxy() as g:
            g.write_note("alpha.md", "# Alpha\nContent.")
            with self._dev():
                result = server.execute_dev_operation("graph.rebuild", {})
        self.assertEqual([n["label"] for n in result["graph"]["nodes"]], ["Alpha"])

    def test_external_operations_are_forbidden(self):
        with TempGalaxy():
            with self._dev():
                for operation in ("web.search", "chat.ask"):
                    with self.assertRaises(server._RequestError) as ctx:
                        server.execute_dev_operation(operation, {})
                    self.assertEqual(ctx.exception.status, 403)

    def test_unknown_operation_raises_400(self):
        with TempGalaxy():
            with self._dev():
                with self.assertRaises(server._RequestError) as ctx:
                    server.execute_dev_operation("notes.explode", {})
        self.assertEqual(ctx.exception.status, 400)

    def test_non_dict_payload_is_tolerated(self):
        with TempGalaxy():
            with self._dev():
                result = server.execute_dev_operation("notes.list", "not-a-dict")
        self.assertEqual(result["items"], [])


if __name__ == "__main__":
    unittest.main()

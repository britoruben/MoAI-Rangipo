# -*- coding: utf-8 -*-
"""Offline tests for server.py — no network, no API calls."""

import io
import json
import os
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import build
import server


class TestNormalize(unittest.TestCase):
    def test_lowercases(self):
        self.assertEqual(server.normalize("HOLA"), "hola")

    def test_strips_tilde_a(self):
        self.assertEqual(server.normalize("canción"), "cancion")

    def test_strips_tilde_e(self):
        self.assertEqual(server.normalize("él"), "el")

    def test_strips_diaeresis(self):
        self.assertEqual(server.normalize("güero"), "guero")

    def test_preserves_ascii(self):
        self.assertEqual(server.normalize("hello123"), "hello123")


class TestTokenize(unittest.TestCase):
    def test_removes_spanish_stopwords(self):
        tokens = server.tokenize("el proyecto de la galaxia")
        self.assertNotIn("el", tokens)
        self.assertNotIn("de", tokens)
        self.assertNotIn("la", tokens)
        self.assertIn("proyecto", tokens)
        self.assertIn("galaxia", tokens)

    def test_normalizes_accents(self):
        tokens = server.tokenize("El volcán Rano Raraku")
        self.assertIn("volcan", tokens)
        self.assertIn("rano", tokens)
        self.assertIn("raraku", tokens)

    def test_excludes_words_under_3_chars(self):
        tokens = server.tokenize("yo lo vi")
        self.assertEqual(tokens, [])

    def test_returns_list(self):
        self.assertIsInstance(server.tokenize("nota sobre galaxia"), list)


class TestScoreNodes(unittest.TestCase):
    def _graph(self, nodes):
        return {"nodes": [dict(id=i, **n) for i, n in enumerate(nodes)]}

    def test_title_match_outranks_excerpt_only(self):
        graph = self._graph([
            {"label": "Rano Raraku",  "type": "note", "excerpt": "general volcanic content"},
            {"label": "Other Note",   "type": "note", "excerpt": "rano raraku stone quarry"},
        ])
        result = server.score_nodes("rano raraku", graph)
        self.assertGreater(len(result), 0)
        self.assertEqual(result[0], 0, "title match should rank first")

    def test_no_match_returns_empty(self):
        graph = self._graph([
            {"label": "Alpha", "type": "note", "excerpt": "beta content here"},
        ])
        self.assertEqual(server.score_nodes("xyzzyx", graph), [])

    def test_returns_at_most_top_nodes(self):
        nodes = [
            {"label": "volcán %d" % i, "type": "note", "excerpt": "volcán content"}
            for i in range(10)
        ]
        graph = self._graph(nodes)
        result = server.score_nodes("volcán", graph)
        self.assertLessEqual(len(result), server.TOP_NODES)

    def test_spanish_connector_synonym_matches_connector_type(self):
        graph = self._graph([
            {"label": "Gmail",   "type": "connector", "excerpt": "correo personal"},
            {"label": "Unrelated note", "type": "note", "excerpt": "algo distinto"},
        ])
        result = server.score_nodes("que conectores tengo configurados", graph)
        self.assertIn(0, result)

    def test_spanish_tool_synonym_matches_tool_type(self):
        graph = self._graph([
            {"label": "Web search", "type": "tool", "excerpt": "busca en internet"},
        ])
        result = server.score_nodes("que herramientas tienes", graph)
        self.assertEqual(result, [0])


class TestParseMarker(unittest.TestCase):
    def test_nodes_marker_extracted(self):
        text, mtype = server.parse_marker("[[nodes]] The galaxy has 5 stars.")
        self.assertEqual(mtype, "nodes")
        self.assertNotIn("[[nodes]]", text)
        self.assertIn("galaxy", text)

    def test_chat_marker_extracted(self):
        text, mtype = server.parse_marker("[[chat]] Hello there, Matatoa.")
        self.assertEqual(mtype, "chat")
        self.assertNotIn("[[chat]]", text)
        self.assertIn("Hello", text)

    def test_web_marker_extracted(self):
        text, mtype = server.parse_marker("[[web]] Found on the web today.")
        self.assertEqual(mtype, "web")
        self.assertNotIn("[[web]]", text)

    def test_fallback_web_when_has_sources(self):
        text, mtype = server.parse_marker("No marker in this answer.", has_sources=True)
        self.assertEqual(mtype, "web")

    def test_fallback_nodes_by_default(self):
        text, mtype = server.parse_marker("No marker here at all.", has_sources=False)
        self.assertEqual(mtype, "nodes")

    def test_pseudo_markers_stripped(self):
        text, mtype = server.parse_marker("[[nodes]] text [[random-thing]] end")
        self.assertNotIn("[[random-thing]]", text)
        self.assertIn("text", text)
        self.assertIn("end", text)

    def test_case_insensitive(self):
        _, mtype = server.parse_marker("[[NODES]] content")
        self.assertEqual(mtype, "nodes")

    def test_marker_with_spaces(self):
        _, mtype = server.parse_marker("[[ nodes ]] content")
        self.assertEqual(mtype, "nodes")


class TestSlugify(unittest.TestCase):
    def test_basic_slug(self):
        self.assertEqual(server.slugify("El volcán Rano"), "el-volcan-rano")

    def test_respects_max_words(self):
        result = server.slugify("one two three four five six seven", max_words=3)
        self.assertEqual(result, "one-two-three")

    def test_empty_input_falls_back(self):
        self.assertEqual(server.slugify(""), "recuerdo")

    def test_special_chars_removed(self):
        result = server.slugify("hello! world?")
        self.assertNotIn("!", result)
        self.assertNotIn("?", result)


class TestTitleFrom(unittest.TestCase):
    def test_capitalizes_first_letter(self):
        result = server.title_from("lowercase start")
        self.assertTrue(result[0].isupper())

    def test_truncates_with_ellipsis(self):
        result = server.title_from("one two three four five six seven eight nine ten",
                                   max_words=5)
        self.assertIn("…", result)

    def test_short_text_no_ellipsis(self):
        result = server.title_from("short text", max_words=9)
        self.assertNotIn("…", result)

    def test_empty_falls_back(self):
        result = server.title_from("")
        self.assertEqual(result, "Recuerdo")


class _TempGalaxy:
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
        with open(self.connectors_file, "w", encoding="utf-8") as f:
            json.dump({"connectors": []}, f)
        with open(self.tools_file, "w", encoding="utf-8") as f:
            json.dump({"tools": []}, f)

    def write_note(self, rel_path, content):
        abs_path = os.path.join(self.docs_dir, rel_path)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(content)

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


class TestFindNotes(unittest.TestCase):
    def test_lists_everything_with_no_query(self):
        with _TempGalaxy() as g:
            g.write_note("alpha.md", "# Alpha\nSomething about volcanoes.")
            g.write_note("beta.md", "# Beta\nSomething about the sea.")
            build.build()
            notes = server.find_notes(None)
        self.assertEqual({n["label"] for n in notes}, {"Alpha", "Beta"})

    def test_query_filters_by_title(self):
        with _TempGalaxy() as g:
            g.write_note("alpha.md", "# Alpha volcano\nContent.")
            g.write_note("beta.md", "# Beta sea\nContent.")
            build.build()
            notes = server.find_notes("volcano")
        self.assertEqual([n["label"] for n in notes], ["Alpha volcano"])

    def test_unmatched_query_returns_empty(self):
        with _TempGalaxy() as g:
            g.write_note("alpha.md", "# Alpha\nContent.")
            build.build()
            notes = server.find_notes("xyzzyx-nothing-like-this")
        self.assertEqual(notes, [])

    def test_excludes_connectors_and_tools(self):
        with _TempGalaxy() as g:
            g.write_note("alpha.md", "# Alpha\nContent.")
            with open(g.connectors_file, "w", encoding="utf-8") as f:
                json.dump({"connectors": [{"id": "gmail", "label": "Gmail",
                                            "status": "active", "description": "mail"}]}, f)
            build.build()
            notes = server.find_notes(None)
        self.assertEqual([n["label"] for n in notes], ["Alpha"])


class TestDeleteNoteByTitle(unittest.TestCase):
    def test_deletes_matching_note(self):
        with _TempGalaxy() as g:
            g.write_note("alpha.md", "# Alpha volcano\nContent.")
            build.build()
            result = server.delete_note_by_title("Alpha volcano")
            self.assertEqual(result["title"], "Alpha volcano")
            self.assertFalse(os.path.isfile(os.path.join(g.docs_dir, "alpha.md")))
            self.assertEqual(server.find_notes(None), [])

    def test_no_match_raises(self):
        with _TempGalaxy() as g:
            g.write_note("alpha.md", "# Alpha\nContent.")
            build.build()
            with self.assertRaises(RuntimeError):
                server.delete_note_by_title("does-not-exist")

    def test_ambiguous_match_raises(self):
        with _TempGalaxy() as g:
            g.write_note("a.md", "# Volcano Alpha\nContent.")
            g.write_note("b.md", "# Volcano Beta\nContent.")
            build.build()
            with self.assertRaises(RuntimeError):
                server.delete_note_by_title("Volcano")

    def test_empty_title_raises(self):
        with _TempGalaxy():
            with self.assertRaises(RuntimeError):
                server.delete_note_by_title("")


class TestManageEntity(unittest.TestCase):
    def test_create_then_appears_in_items(self):
        with _TempGalaxy() as g:
            result = server.manage_entity(
                g.connectors_file, "connectors", "create", "gmail",
                {"label": "Gmail", "status": "active", "description": "mail"},
            )
        self.assertEqual([i["id"] for i in result["items"]], ["gmail"])

    def test_create_duplicate_raises(self):
        with _TempGalaxy() as g:
            server.manage_entity(g.connectors_file, "connectors", "create", "gmail", {})
            with self.assertRaises(RuntimeError):
                server.manage_entity(g.connectors_file, "connectors", "create", "gmail", {})

    def test_create_invalid_id_raises(self):
        with _TempGalaxy() as g:
            with self.assertRaises(RuntimeError):
                server.manage_entity(g.connectors_file, "connectors", "create", "Bad Id!", {})

    def test_update_changes_fields(self):
        with _TempGalaxy() as g:
            server.manage_entity(g.connectors_file, "connectors", "create", "gmail",
                                  {"label": "Gmail", "description": "old"})
            result = server.manage_entity(g.connectors_file, "connectors", "update", "gmail",
                                           {"description": "new"})
        updated = next(i for i in result["items"] if i["id"] == "gmail")
        self.assertEqual(updated["description"], "new")

    def test_update_missing_raises(self):
        with _TempGalaxy() as g:
            with self.assertRaises(RuntimeError):
                server.manage_entity(g.connectors_file, "connectors", "update", "nope", {})

    def test_delete_removes_entry(self):
        with _TempGalaxy() as g:
            server.manage_entity(g.connectors_file, "connectors", "create", "gmail", {})
            result = server.manage_entity(g.connectors_file, "connectors", "delete", "gmail", {})
        self.assertEqual(result["items"], [])

    def test_delete_missing_raises(self):
        with _TempGalaxy() as g:
            with self.assertRaises(RuntimeError):
                server.manage_entity(g.connectors_file, "connectors", "delete", "nope", {})

    def test_create_rebuilds_graph_with_new_node(self):
        with _TempGalaxy() as g:
            result = server.manage_entity(
                g.connectors_file, "connectors", "create", "gmail", {"label": "Gmail"}
            )
        labels = {n["label"] for n in result["graph"]["nodes"]}
        self.assertIn("Gmail", labels)


class TestDevMode(unittest.TestCase):
    def test_dev_mode_executes_real_local_operations(self):
        with _TempGalaxy() as g:
            g.write_note("alpha.md", "# Alpha\nOriginal.")
            build.build()
            with unittest.mock.patch.object(server, "RUNTIME_MODE", "dev"):
                listed = server.execute_dev_operation("notes.list", {})
                self.assertEqual([n["label"] for n in listed["items"]], ["Alpha"])

                updated = server.execute_dev_operation("notes.update", {
                    "path": "docs/alpha.md",
                    "content": "# Alpha\nUpdated.\n",
                })
                self.assertIsNotNone(updated["node_id"])
                with open(os.path.join(g.docs_dir, "alpha.md"), encoding="utf-8") as f:
                    self.assertIn("Updated.", f.read())

                created = server.execute_dev_operation("connectors.create", {
                    "id": "gmail", "label": "Gmail", "status": "placeholder",
                })
                self.assertEqual(created["items"][0]["id"], "gmail")

    def test_dev_mode_delete_is_real_and_production_is_blocked(self):
        with _TempGalaxy() as g:
            g.write_note("alpha.md", "# Alpha\nOriginal.")
            build.build()
            with unittest.mock.patch.object(server, "RUNTIME_MODE", "dev"):
                server.execute_dev_operation("notes.delete", {"path": "docs/alpha.md"})
                self.assertFalse(os.path.exists(os.path.join(g.docs_dir, "alpha.md")))
            with unittest.mock.patch.object(server, "RUNTIME_MODE", "production"):
                with self.assertRaises(server._RequestError):
                    server.execute_dev_operation("notes.list", {})


class TestPreferences(unittest.TestCase):
    def test_missing_file_returns_none_sentinel(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "preferences.json")
            with unittest.mock.patch.object(server, "PREFERENCES_FILE", path):
                self.assertEqual(server.load_preferences(), {"lang": None, "name": None})

    def test_invalid_json_returns_none_sentinel(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "preferences.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("not json")
            with unittest.mock.patch.object(server, "PREFERENCES_FILE", path):
                self.assertEqual(server.load_preferences(), {"lang": None, "name": None})

    def test_save_then_load_round_trips(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "preferences.json")
            with unittest.mock.patch.object(server, "PREFERENCES_FILE", path):
                server.save_preferences("en", "Alex")
                self.assertEqual(server.load_preferences(), {"lang": "en", "name": "Alex"})

    def test_rejects_invalid_lang(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "preferences.json")
            with unittest.mock.patch.object(server, "PREFERENCES_FILE", path):
                with self.assertRaises(server._RequestError):
                    server.save_preferences("fr", "Alex")

    def test_rejects_empty_name(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "preferences.json")
            with unittest.mock.patch.object(server, "PREFERENCES_FILE", path):
                with self.assertRaises(server._RequestError):
                    server.save_preferences("es", "   ")


class TestBuildSystemPrompt(unittest.TestCase):
    GRAPH = {"nodes": [
        {"id": 0, "type": "note", "label": "Alpha", "excerpt": "About volcanoes."},
    ]}

    def test_english_prompt_addresses_chosen_name(self):
        prompt = server.build_system_prompt(self.GRAPH, [0], "en", "Alex")
        self.assertIn("Alex", prompt)
        self.assertIn("You are Moai", prompt)
        self.assertNotIn("Matatoa", prompt)

    def test_spanish_prompt_addresses_chosen_name(self):
        prompt = server.build_system_prompt(self.GRAPH, [0], "es", "Alex")
        self.assertIn("Alex", prompt)
        self.assertIn("Eres Moai", prompt)

    def test_both_languages_carry_the_same_markers(self):
        for lang in ("es", "en"):
            prompt = server.build_system_prompt(self.GRAPH, [0], lang, "Alex")
            self.assertIn(server.MARK_NODES, prompt)
            self.assertIn(server.MARK_CHAT, prompt)
            self.assertIn(server.MARK_WEB, prompt)

    def test_no_mojibake_in_spanish_prompt(self):
        prompt = server.build_system_prompt(self.GRAPH, [0], "es", "Alex")
        self.assertNotIn("Ã", prompt)
        self.assertIn("menú", prompt)


class TestLoadConfig(unittest.TestCase):
    def _with_config(self, content):
        td = tempfile.TemporaryDirectory()
        path = os.path.join(td.name, "config.json")
        if content is not None:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        return td, unittest.mock.patch.object(server, "CONFIG_FILE", path)

    def test_missing_file_raises_actionable_request_error(self):
        td, patch = self._with_config(None)
        with td, patch:
            with self.assertRaises(server._RequestError) as ctx:
                server.load_config()
        self.assertEqual(ctx.exception.status, 500)
        self.assertIn("config.example.json", str(ctx.exception))

    def test_invalid_json_raises_request_error(self):
        td, patch = self._with_config("{not json")
        with td, patch:
            with self.assertRaises(server._RequestError) as ctx:
                server.load_config()
        self.assertIn("not valid JSON", str(ctx.exception))

    def test_non_object_root_raises_request_error(self):
        td, patch = self._with_config("[]")
        with td, patch:
            with self.assertRaises(server._RequestError):
                server.load_config()

    def test_valid_config_is_returned(self):
        td, patch = self._with_config('{"api_key": "k"}')
        with td, patch:
            self.assertEqual(server.load_config(), {"api_key": "k"})


class TestLoadGraph(unittest.TestCase):
    def test_missing_file_raises_build_error(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "graph-data.js")
            with unittest.mock.patch.object(server, "GRAPH_FILE", path):
                server._graph_cache["stamp"] = None
                with self.assertRaises(server._BuildError):
                    server.load_graph()

    def test_unparseable_file_raises_build_error(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "graph-data.js")
            with open(path, "w", encoding="utf-8") as f:
                f.write("const GRAPH = {broken")
            with unittest.mock.patch.object(server, "GRAPH_FILE", path):
                server._graph_cache["stamp"] = None
                with self.assertRaises(server._BuildError) as ctx:
                    server.load_graph()
        self.assertIn("build.py", str(ctx.exception))

    def test_rebuild_in_same_timestamp_tick_is_not_served_from_cache(self):
        with _TempGalaxy() as g:
            g.write_note("alpha.md", "# Alpha\nContent.")
            build.build()
            self.assertEqual(len(server.load_graph()["nodes"]), 1)
            os.remove(os.path.join(g.docs_dir, "alpha.md"))
            server._rebuild_galaxy()
            self.assertEqual(server.load_graph()["nodes"], [])


class TestRebuildGalaxy(unittest.TestCase):
    def test_build_failure_becomes_build_error_with_cause(self):
        boom = OSError("disk on fire")
        with unittest.mock.patch.object(build, "build", side_effect=boom):
            with self.assertRaises(server._BuildError) as ctx:
                server._rebuild_galaxy()
        self.assertIs(ctx.exception.__cause__, boom)
        self.assertIsNone(server._graph_cache["stamp"])

    def test_build_error_is_a_runtime_error(self):
        # existing callers catch RuntimeError; they must keep working
        self.assertTrue(issubclass(server._BuildError, RuntimeError))
        self.assertTrue(issubclass(server._UpstreamError, RuntimeError))


class _StubHandler(server.MoaiHandler):
    """MoaiHandler without a socket: enough of it to exercise _read_body and
    _guard, which are where request-level errors are turned into responses."""

    def __init__(self, body=b"", headers=None, command="POST", path="/test"):
        self.rfile = io.BytesIO(body)
        self.headers = headers if headers is not None else {
            "Content-Length": str(len(body))
        }
        self.command = command
        self.path = path
        self.sent = []

    def _send_json(self, obj, status=200):
        self.sent.append((status, obj))

    def log_message(self, *a):
        pass


class TestReadBody(unittest.TestCase):
    def test_parses_json_object(self):
        self.assertEqual(_StubHandler(b'{"a": 1}')._read_body(), {"a": 1})

    def test_invalid_json_is_400(self):
        with self.assertRaises(server._RequestError) as ctx:
            _StubHandler(b"{nope")._read_body()
        self.assertEqual(ctx.exception.status, 400)

    def test_non_object_body_is_400(self):
        with self.assertRaises(server._RequestError) as ctx:
            _StubHandler(b"[1, 2]")._read_body()
        self.assertEqual(ctx.exception.status, 400)

    def test_invalid_utf8_is_400(self):
        with self.assertRaises(server._RequestError) as ctx:
            _StubHandler(b"\xff\xfe")._read_body()
        self.assertEqual(ctx.exception.status, 400)

    def test_bad_content_length_is_400(self):
        handler = _StubHandler(b"{}", headers={"Content-Length": "abc"})
        with self.assertRaises(server._RequestError) as ctx:
            handler._read_body()
        self.assertEqual(ctx.exception.status, 400)

    def test_oversized_body_is_413(self):
        with self.assertRaises(server._RequestError) as ctx:
            _StubHandler(b'{"a": 1}')._read_body(max_bytes=2)
        self.assertEqual(ctx.exception.status, 413)


class TestGuard(unittest.TestCase):
    def _status_for(self, exc):
        handler = _StubHandler()

        def route():
            raise exc

        with unittest.mock.patch.object(server, "log_exception"):
            handler._guard(route)
        self.assertEqual(len(handler.sent), 1)
        return handler.sent[0]

    def test_request_error_keeps_its_status(self):
        status, body = self._status_for(server._RequestError("nope", 404))
        self.assertEqual(status, 404)
        self.assertEqual(body, {"error": "nope"})

    def test_upstream_error_is_502(self):
        status, _ = self._status_for(server._UpstreamError("api down"))
        self.assertEqual(status, 502)

    def test_build_error_is_500(self):
        status, _ = self._status_for(server._BuildError("no galaxy"))
        self.assertEqual(status, 500)

    def test_domain_runtime_error_is_409(self):
        status, body = self._status_for(RuntimeError("no note matches 'x'"))
        self.assertEqual(status, 409)
        self.assertEqual(body, {"error": "no note matches 'x'"})

    def test_unexpected_error_is_a_generic_500(self):
        status, body = self._status_for(KeyError("nodes"))
        self.assertEqual(status, 500)
        self.assertNotIn("nodes", body["error"])

    def test_successful_route_is_left_alone(self):
        handler = _StubHandler()
        handler._guard(lambda: handler._send_json({"ok": True}))
        self.assertEqual(handler.sent, [(200, {"ok": True})])


if __name__ == "__main__":
    unittest.main()

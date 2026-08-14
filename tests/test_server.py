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
from tests.galaxy import TempGalaxy as _TempGalaxy


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


class TestUndoDelete(unittest.TestCase):
    def test_undo_restores_the_deleted_note_verbatim(self):
        with _TempGalaxy() as g:
            g.write_note("alpha.md", "# Alpha volcano\nOriginal content.")
            build.build()
            server.delete_note_by_title("Alpha volcano")
            self.assertFalse(os.path.isfile(os.path.join(g.docs_dir, "alpha.md")))

            result = server.undo_last_delete()

            self.assertEqual(result["path"], "docs/alpha.md")
            self.assertIsNotNone(result["node_id"])
            with open(os.path.join(g.docs_dir, "alpha.md"), encoding="utf-8") as f:
                self.assertEqual(f.read(), "# Alpha volcano\nOriginal content.")
            self.assertEqual(
                [n["label"] for n in server.find_notes(None)], ["Alpha volcano"]
            )

    def test_undo_with_nothing_to_undo_raises(self):
        with _TempGalaxy():
            with self.assertRaises(RuntimeError) as ctx:
                server.undo_last_delete()
        self.assertIn("Nothing to undo", str(ctx.exception))

    def test_undo_slot_is_consumed_after_use(self):
        with _TempGalaxy() as g:
            g.write_note("alpha.md", "# Alpha\nContent.")
            build.build()
            server.delete_note_by_title("Alpha")
            server.undo_last_delete()
            with self.assertRaises(RuntimeError) as ctx:
                server.undo_last_delete()
        self.assertIn("Nothing to undo", str(ctx.exception))

    def test_only_the_most_recent_delete_is_recoverable(self):
        with _TempGalaxy() as g:
            g.write_note("alpha.md", "# Alpha\nFirst.")
            g.write_note("beta.md", "# Beta\nSecond.")
            build.build()
            server.delete_note_by_title("Alpha")
            server.delete_note_by_title("Beta")  # overwrites the undo slot

            result = server.undo_last_delete()

            self.assertEqual(result["path"], "docs/beta.md")
            self.assertFalse(os.path.isfile(os.path.join(g.docs_dir, "alpha.md")))
            self.assertTrue(os.path.isfile(os.path.join(g.docs_dir, "beta.md")))
            with self.assertRaises(RuntimeError):
                server.undo_last_delete()  # alpha is gone for good

    def test_undo_refuses_to_overwrite_a_note_recreated_at_the_same_path(self):
        with _TempGalaxy() as g:
            g.write_note("alpha.md", "# Alpha\nOriginal.")
            build.build()
            server.delete_note_by_title("Alpha")
            g.write_note("alpha.md", "# Alpha\nA brand new note, same filename.")
            build.build()

            with self.assertRaises(RuntimeError) as ctx:
                server.undo_last_delete()
        self.assertIn("already exists", str(ctx.exception))

    def test_undo_works_across_every_deletion_entry_point(self):
        # delete_note_by_path (Dev console / update_note_by_path's sibling)
        # and the plain DELETE /note handler share the same undo buffer via
        # _stash_and_remove — not just the delete_note chat tool.
        with _TempGalaxy() as g:
            g.write_note("alpha.md", "# Alpha\nContent.")
            build.build()
            server.delete_note_by_path("docs/alpha.md")
            result = server.undo_last_delete()
        self.assertEqual(result["path"], "docs/alpha.md")


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
            with unittest.mock.patch.object(server, "RUNTIME_MODE", "dev"), \
                 unittest.mock.patch.object(server, "dev_mode_allowed", return_value=True):
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
            with unittest.mock.patch.object(server, "RUNTIME_MODE", "dev"), \
                 unittest.mock.patch.object(server, "dev_mode_allowed", return_value=True):
                server.execute_dev_operation("notes.delete", {"path": "docs/alpha.md"})
                self.assertFalse(os.path.exists(os.path.join(g.docs_dir, "alpha.md")))
            with unittest.mock.patch.object(server, "RUNTIME_MODE", "production"), \
                 unittest.mock.patch.object(server, "dev_mode_allowed", return_value=True):
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
    """MoaiHandler without a socket: enough of it to exercise _read_body,
    _guard, and _check_origin, which are where request-level errors are
    turned into responses."""

    def __init__(self, body=b"", headers=None, command="POST", path="/test"):
        self.rfile = io.BytesIO(body)
        self.headers = headers if headers is not None else {
            "Content-Length": str(len(body)),
            "Host": "127.0.0.1:%d" % server.PORT,
        }
        self.command = command
        self.path = path
        self.sent = []
        # _check_origin derives its allowlist from the bound port
        self.server = unittest.mock.Mock(server_address=("127.0.0.1", server.PORT))

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

    def test_wrong_content_type_is_415(self):
        handler = _StubHandler(b'{"a": 1}', headers={
            "Content-Length": "8", "Content-Type": "text/plain",
        })
        with self.assertRaises(server._RequestError) as ctx:
            handler._read_body()
        self.assertEqual(ctx.exception.status, 415)

    def test_json_content_type_is_accepted(self):
        handler = _StubHandler(b'{"a": 1}', headers={
            "Content-Length": "8", "Content-Type": "application/json; charset=utf-8",
        })
        self.assertEqual(handler._read_body(), {"a": 1})

    def test_no_content_type_is_accepted(self):
        # curl and same-origin fetch() without an explicit header both omit it
        self.assertEqual(_StubHandler(b'{"a": 1}')._read_body(), {"a": 1})


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


class TestCheckOrigin(unittest.TestCase):
    """_check_origin / _enforce_origin: the DNS-rebinding and cross-site
    guard that runs first in every do_* (see ARCHITECTURE.md §5)."""

    def test_legitimate_localhost_host_passes(self):
        handler = _StubHandler(headers={"Host": "localhost:%d" % server.PORT})
        handler._check_origin()  # must not raise

    def test_legitimate_127_0_0_1_host_passes(self):
        handler = _StubHandler(headers={"Host": "127.0.0.1:%d" % server.PORT})
        handler._check_origin()  # must not raise

    def test_forged_host_is_403(self):
        handler = _StubHandler(headers={"Host": "evil.example:%d" % server.PORT})
        with self.assertRaises(server._RequestError) as ctx:
            handler._check_origin()
        self.assertEqual(ctx.exception.status, 403)

    def test_missing_host_is_403(self):
        handler = _StubHandler(headers={})
        with self.assertRaises(server._RequestError) as ctx:
            handler._check_origin()
        self.assertEqual(ctx.exception.status, 403)

    def test_cross_origin_origin_header_is_403(self):
        handler = _StubHandler(headers={
            "Host": "127.0.0.1:%d" % server.PORT,
            "Origin": "http://evil.example",
        })
        with self.assertRaises(server._RequestError) as ctx:
            handler._check_origin()
        self.assertEqual(ctx.exception.status, 403)

    def test_same_origin_origin_header_passes(self):
        handler = _StubHandler(headers={
            "Host": "127.0.0.1:%d" % server.PORT,
            "Origin": "http://127.0.0.1:%d" % server.PORT,
        })
        handler._check_origin()  # must not raise

    def test_cross_site_sec_fetch_site_is_403(self):
        handler = _StubHandler(headers={
            "Host": "127.0.0.1:%d" % server.PORT,
            "Sec-Fetch-Site": "cross-site",
        })
        with self.assertRaises(server._RequestError) as ctx:
            handler._check_origin()
        self.assertEqual(ctx.exception.status, 403)

    def test_same_origin_sec_fetch_site_passes(self):
        handler = _StubHandler(headers={
            "Host": "127.0.0.1:%d" % server.PORT,
            "Sec-Fetch-Site": "same-origin",
        })
        handler._check_origin()  # must not raise

    def test_headerless_client_passes(self):
        # curl and the offline test suite itself send no Origin/Sec-Fetch-Site
        handler = _StubHandler(headers={"Host": "127.0.0.1:%d" % server.PORT})
        handler._check_origin()  # must not raise

    def test_enforce_origin_sends_403_and_returns_false(self):
        handler = _StubHandler(headers={"Host": "evil.example"})
        self.assertFalse(handler._enforce_origin())
        self.assertEqual(len(handler.sent), 1)
        self.assertEqual(handler.sent[0][0], 403)

    def test_enforce_origin_returns_true_on_success(self):
        handler = _StubHandler(headers={"Host": "127.0.0.1:%d" % server.PORT})
        self.assertTrue(handler._enforce_origin())
        self.assertEqual(handler.sent, [])


class TestDevModeAllowed(unittest.TestCase):
    """Dev mode must never be armable purely over HTTP — only by how the
    process itself was started (MOAI_DEV=1 or --dev)."""

    def test_disabled_by_default(self):
        with unittest.mock.patch.object(sys, "argv", ["server.py"]), \
             unittest.mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(server.dev_mode_allowed())

    def test_env_var_enables_it(self):
        with unittest.mock.patch.object(sys, "argv", ["server.py"]), \
             unittest.mock.patch.dict(os.environ, {"MOAI_DEV": "1"}):
            self.assertTrue(server.dev_mode_allowed())

    def test_dev_flag_enables_it(self):
        with unittest.mock.patch.object(sys, "argv", ["server.py", "--dev"]), \
             unittest.mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(server.dev_mode_allowed())

    def test_set_runtime_mode_blocks_dev_when_not_allowed(self):
        with unittest.mock.patch.object(sys, "argv", ["server.py"]), \
             unittest.mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(server._RequestError) as ctx:
                server.set_runtime_mode("dev")
        self.assertEqual(ctx.exception.status, 403)

    def test_execute_dev_operation_blocks_when_not_allowed(self):
        with unittest.mock.patch.object(sys, "argv", ["server.py"]), \
             unittest.mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(server._RequestError) as ctx:
                server.execute_dev_operation("notes.list", {})
        self.assertEqual(ctx.exception.status, 403)


if __name__ == "__main__":
    unittest.main()

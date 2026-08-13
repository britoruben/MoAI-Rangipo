# -*- coding: utf-8 -*-
"""Offline tests for MoaiHandler: every HTTP endpoint is exercised against a
real ThreadingHTTPServer bound to an ephemeral port, over a temp galaxy.
Anthropic and ElevenLabs are always mocked — nothing leaves the machine."""

import json
import os
import sys
import threading
import unittest
import unittest.mock
import urllib.error
import urllib.parse
import urllib.request
from functools import partial
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import build
import server
from tests.galaxy import TempGalaxy


class _Server:
    """Context manager: a TempGalaxy plus a live MoaiHandler serving it."""

    def __init__(self):
        self.galaxy = TempGalaxy()

    def __enter__(self):
        self.galaxy.__enter__()
        self.viewer_dir = os.path.join(self.galaxy.root, "viewer")
        os.makedirs(self.viewer_dir, exist_ok=True)
        handler = partial(server.MoaiHandler, directory=self.viewer_dir)
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.base = "http://127.0.0.1:%d" % self._httpd.server_address[1]
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self._thread.start()
        return self

    def __exit__(self, *a):
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)
        return self.galaxy.__exit__(*a)

    def request(self, method, path, body=None, raw_body=None, headers=None):
        """Returns (status, parsed_json_or_text)."""
        data = raw_body if raw_body is not None else (
            json.dumps(body).encode("utf-8") if body is not None else None
        )
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, self._parse(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, self._parse(e.read())

    @staticmethod
    def _parse(payload):
        text = payload.decode("utf-8")
        try:
            return json.loads(text)
        except ValueError:
            return text


class TestGetEndpoints(unittest.TestCase):
    def test_runtime_reports_current_mode(self):
        with _Server() as s:
            status, body = s.request("GET", "/runtime")
        self.assertEqual(status, 200)
        self.assertEqual(body["mode"], server.runtime_status()["mode"])

    def test_preferences_returns_stored_values(self):
        with _Server() as s:
            s.galaxy.write_json(s.galaxy.preferences_file, {"lang": "en", "name": "Alex"})
            status, body = s.request("GET", "/preferences")
        self.assertEqual((status, body), (200, {"lang": "en", "name": "Alex"}))

    def test_models_exposes_ids_but_not_the_api_key(self):
        with _Server() as s:
            s.galaxy.write_json(s.galaxy.config_file, {
                "api_key": "sk-secret", "model": "claude-haiku-4-5",
                "models": [{"id": "claude-sonnet-5"}],
            })
            status, body = s.request("GET", "/models")
        self.assertEqual(status, 200)
        self.assertEqual(body["default"], "claude-haiku-4-5")
        self.assertNotIn("sk-secret", json.dumps(body))

    def test_models_without_config_returns_500(self):
        with _Server() as s:
            status, body = s.request("GET", "/models")
        self.assertEqual(status, 500)
        self.assertIn("error", body)

    def test_powers_merges_builtins_connectors_and_tools(self):
        with _Server() as s:
            s.galaxy.write_json(s.galaxy.connectors_file, {"connectors": [{"id": "gmail"}]})
            status, body = s.request("GET", "/powers")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["actions"]), len(server.BUILTIN_POWERS))
        self.assertEqual(body["integration_summary"], {"connectors": 1, "tools": 0})
        self.assertEqual([c["id"] for c in body["connectors"]], ["gmail"])

    def test_notes_search_filters_by_query(self):
        with _Server() as s:
            s.galaxy.write_note("alpha.md", "# Alpha volcano\nContent.")
            s.galaxy.write_note("beta.md", "# Beta sea\nContent.")
            build.build()
            status, body = s.request("GET", "/notes?q=volcano")
        self.assertEqual(status, 200)
        self.assertEqual([n["label"] for n in body["notes"]], ["Alpha volcano"])

    def test_connectors_and_tools_listings(self):
        with _Server() as s:
            s.galaxy.write_json(s.galaxy.tools_file, {"tools": [{"id": "web-search"}]})
            self.assertEqual(s.request("GET", "/connectors")[1], {"connectors": []})
            self.assertEqual(s.request("GET", "/tools")[1],
                             {"tools": [{"id": "web-search"}]})

    def test_note_returns_raw_markdown(self):
        with _Server() as s:
            s.galaxy.write_note("alpha.md", "# Alpha\nBody.")
            status, body = s.request("GET", "/note?path=docs/alpha.md")
        self.assertEqual(status, 200)
        self.assertEqual(body["content"], "# Alpha\nBody.")

    def test_note_requires_a_path(self):
        with _Server() as s:
            status, body = s.request("GET", "/note")
        self.assertEqual(status, 400)
        self.assertIn("path", body["error"])

    def test_note_unknown_path_returns_404(self):
        with _Server() as s:
            status, _body = s.request("GET", "/note?path=docs/ghost.md")
        self.assertEqual(status, 404)

    def test_note_outside_docs_returns_403(self):
        with _Server() as s:
            status, _body = s.request("GET", "/note?path=config.json.md")
        self.assertEqual(status, 403)

    def test_static_files_are_served_without_caching(self):
        with _Server() as s:
            with open(os.path.join(s.viewer_dir, "index.html"), "w", encoding="utf-8") as f:
                f.write("<h1>MoAI</h1>")
            req = urllib.request.Request(s.base + "/index.html")
            with urllib.request.urlopen(req, timeout=10) as resp:
                self.assertEqual(resp.headers["Cache-Control"], "no-store")
                self.assertIn("MoAI", resp.read().decode("utf-8"))


class TestRuntimeAndPreferencesPost(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, server, "RUNTIME_MODE", server.RUNTIME_MODE)

    def test_switching_to_dev_and_back(self):
        with _Server() as s:
            self.assertEqual(s.request("POST", "/runtime", {"mode": "dev"})[1]["mode"], "dev")
            self.assertEqual(s.request("GET", "/runtime")[1]["mode"], "dev")
            self.assertEqual(
                s.request("POST", "/runtime", {"mode": "production"})[1]["mode"], "production")

    def test_invalid_mode_returns_400(self):
        with _Server() as s:
            status, body = s.request("POST", "/runtime", {"mode": "staging"})
        self.assertEqual(status, 400)
        self.assertIn("production", body["error"])

    def test_preferences_are_persisted(self):
        with _Server() as s:
            status, body = s.request("POST", "/preferences", {"lang": "en", "name": "Alex"})
            self.assertEqual((status, body), (200, {"lang": "en", "name": "Alex"}))
            with open(s.galaxy.preferences_file, encoding="utf-8") as f:
                self.assertEqual(json.load(f), {"lang": "en", "name": "Alex"})

    def test_invalid_preferences_return_400(self):
        with _Server() as s:
            self.assertEqual(s.request("POST", "/preferences", {"lang": "fr", "name": "A"})[0], 400)
            self.assertEqual(s.request("POST", "/preferences", {"lang": "en", "name": " "})[0], 400)

    def test_oversized_body_returns_413(self):
        with _Server() as s:
            status, _body = s.request(
                "POST", "/preferences",
                {"lang": "en", "name": "A" * 2000},
            )
        self.assertEqual(status, 413)

    def test_malformed_json_returns_500(self):
        with _Server() as s:
            status, body = s.request("POST", "/runtime", raw_body=b"{not json")
        self.assertEqual(status, 500)
        self.assertIn("error", body)

    def test_unknown_post_path_returns_404(self):
        with _Server() as s:
            self.assertEqual(s.request("POST", "/nope", {})[0], 404)

    def test_unknown_put_and_delete_paths_return_404(self):
        with _Server() as s:
            self.assertEqual(s.request("PUT", "/nope", {})[0], 404)
            self.assertEqual(s.request("DELETE", "/nope")[0], 404)


class TestDevExecuteEndpoint(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, server, "RUNTIME_MODE", server.RUNTIME_MODE)

    def test_dev_operation_runs_when_dev_mode_is_active(self):
        with _Server() as s:
            s.galaxy.write_note("alpha.md", "# Alpha\nContent.")
            build.build()
            s.request("POST", "/runtime", {"mode": "dev"})
            status, body = s.request("POST", "/dev/execute",
                                     {"operation": "notes.list", "payload": {}})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual([n["label"] for n in body["result"]["items"]], ["Alpha"])

    def test_dev_operation_rejected_in_production(self):
        with _Server() as s:
            s.request("POST", "/runtime", {"mode": "production"})
            status, body = s.request("POST", "/dev/execute",
                                     {"operation": "notes.list", "payload": {}})
        self.assertEqual(status, 409)
        self.assertIn("Dev mode", body["error"])


class TestRememberEndpoint(unittest.TestCase):
    def test_writes_a_note_and_returns_the_new_node(self):
        with _Server() as s:
            status, body = s.request("POST", "/remember", {"text": "el volcán despierta"})
            self.assertEqual(status, 200)
            self.assertEqual(body["title"], "El volcán despierta")
            self.assertEqual(len(os.listdir(s.galaxy.captures_dir)), 1)
        self.assertEqual(body["graph"]["nodes"][body["new_id"]]["label"], body["title"])

    def test_empty_text_returns_400(self):
        with _Server() as s:
            status, body = s.request("POST", "/remember", {"text": "   "})
        self.assertEqual(status, 400)
        self.assertIn("nothing to remember", body["error"])

    def test_too_long_text_returns_400(self):
        with _Server() as s:
            status, body = s.request(
                "POST", "/remember", {"text": "x" * (server.MAX_REMEMBER_CHARS + 1)})
        self.assertEqual(status, 400)
        self.assertIn("too long", body["error"])


class TestEditEndpoint(unittest.TestCase):
    def test_overwrites_the_note_and_returns_its_node_id(self):
        with _Server() as s:
            s.galaxy.write_note("alpha.md", "# Alpha\nOld.")
            build.build()
            status, body = s.request("POST", "/edit",
                                     {"path": "docs/alpha.md", "content": "# Alpha\nNew.\n"})
            self.assertEqual(status, 200)
            self.assertIsNotNone(body["node_id"])
            content = s.galaxy.read_note("alpha.md")
        self.assertIn("New.", content)
        self.assertIn("*Last edited:", content)

    def test_missing_path_returns_400(self):
        with _Server() as s:
            self.assertEqual(s.request("POST", "/edit", {"content": "x"})[0], 400)

    def test_unknown_note_returns_404(self):
        with _Server() as s:
            self.assertEqual(
                s.request("POST", "/edit", {"path": "docs/ghost.md", "content": "x"})[0], 404)


class TestNoteDeleteEndpoint(unittest.TestCase):
    def test_deletes_the_file_and_rebuilds_the_graph(self):
        with _Server() as s:
            s.galaxy.write_note("alpha.md", "# Alpha\nContent.")
            build.build()
            status, body = s.request("DELETE", "/note?path=docs/alpha.md")
            self.assertFalse(os.path.exists(os.path.join(s.galaxy.docs_dir, "alpha.md")))
        self.assertEqual(status, 200)
        self.assertEqual(body["graph"]["nodes"], [])

    def test_missing_path_returns_400(self):
        with _Server() as s:
            self.assertEqual(s.request("DELETE", "/note")[0], 400)

    def test_unknown_note_returns_404(self):
        with _Server() as s:
            self.assertEqual(s.request("DELETE", "/note?path=docs/ghost.md")[0], 404)


class TestEntityEndpoints(unittest.TestCase):
    def test_connector_create_update_delete_round_trip(self):
        with _Server() as s:
            status, body = s.request("POST", "/connectors",
                                     {"id": "gmail", "label": "Gmail", "status": "active"})
            self.assertEqual(status, 200)
            self.assertEqual([c["id"] for c in body["items"]], ["gmail"])

            status, body = s.request("PUT", "/connectors",
                                     {"id": "gmail", "status": "placeholder"})
            self.assertEqual((status, body["items"][0]["status"]), (200, "placeholder"))

            status, body = s.request("DELETE", "/connectors?id=gmail")
            self.assertEqual((status, body["items"]), (200, []))

    def test_tool_create_appears_as_a_graph_node(self):
        with _Server() as s:
            status, body = s.request("POST", "/tools", {"id": "web-search", "label": "Web search"})
        self.assertEqual(status, 200)
        self.assertIn("Web search", {n["label"] for n in body["graph"]["nodes"]})

    def test_missing_id_returns_400(self):
        with _Server() as s:
            self.assertEqual(s.request("POST", "/connectors", {"label": "Gmail"})[0], 400)
            self.assertEqual(s.request("DELETE", "/tools")[0], 400)

    def test_conflicting_change_returns_409(self):
        with _Server() as s:
            s.request("POST", "/connectors", {"id": "gmail"})
            status, body = s.request("POST", "/connectors", {"id": "gmail"})
            self.assertEqual(status, 409)
            self.assertIn("already exists", body["error"])
            self.assertEqual(s.request("PUT", "/tools", {"id": "ghost"})[0], 409)


class TestTtsEndpoint(unittest.TestCase):
    def test_not_configured_returns_501(self):
        with _Server() as s:
            status, body = s.request("POST", "/tts", {"text": "hola"})
        self.assertEqual(status, 501)
        self.assertIn("ElevenLabs", body["error"])

    def test_empty_text_returns_400(self):
        with _Server() as s:
            self.assertEqual(s.request("POST", "/tts", {"text": "  "})[0], 400)

    def test_proxies_audio_when_configured(self):
        with _Server() as s:
            s.galaxy.write_json(s.galaxy.config_el_file, {"api_key": "k", "voice_id": "v"})
            with unittest.mock.patch.object(
                server, "text_to_speech", return_value={"audio_base64": "abc"}
            ) as tts:
                status, body = s.request("POST", "/tts", {"text": "hola"})
        self.assertEqual((status, body), (200, {"audio_base64": "abc"}))
        self.assertEqual(tts.call_args[0][0], "hola")

    def test_long_text_is_truncated_before_the_call(self):
        with _Server() as s:
            s.galaxy.write_json(s.galaxy.config_el_file, {"api_key": "k", "voice_id": "v"})
            with unittest.mock.patch.object(server, "text_to_speech", return_value={}) as tts:
                s.request("POST", "/tts", {"text": "a" * (server.MAX_TTS_CHARS + 50)})
        self.assertEqual(len(tts.call_args[0][0]), server.MAX_TTS_CHARS)

    def test_provider_failure_returns_502(self):
        with _Server() as s:
            s.galaxy.write_json(s.galaxy.config_el_file, {"api_key": "k", "voice_id": "v"})
            with unittest.mock.patch.object(
                server, "text_to_speech", side_effect=RuntimeError("quota exceeded")
            ):
                status, body = s.request("POST", "/tts", {"text": "hola"})
        self.assertEqual(status, 502)
        self.assertIn("quota exceeded", body["error"])


class TestChatEndpoint(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, server, "RUNTIME_MODE", server.RUNTIME_MODE)
        self.addCleanup(server._sessions.clear)
        self.addCleanup(server._session_times.clear)
        self.addCleanup(server._session_locks.clear)

    def _configured(self, s):
        s.galaxy.write_json(s.galaxy.config_file, {
            "api_key": "sk-test", "model": "claude-haiku-4-5",
            "models": [{"id": "claude-sonnet-5"}],
        })

    def test_answers_and_keeps_scored_nodes(self):
        with _Server() as s:
            self._configured(s)
            s.galaxy.write_note("volcano.md", "# Volcano\nAbout the volcano.")
            build.build()
            with unittest.mock.patch.object(
                server, "call_claude",
                return_value=("[[nodes]] El volcán duerme.", [], None, ["list_notes"]),
            ) as call:
                status, body = s.request("POST", "/chat",
                                         {"question": "que sabes del volcano",
                                          "session_id": "t1", "model": "claude-sonnet-5"})
        self.assertEqual(status, 200)
        self.assertEqual(body["answer"], "El volcán duerme.")
        self.assertEqual(body["nodes"], [0])
        self.assertEqual(body["model"], "claude-sonnet-5")
        self.assertEqual(body["tools_used"], ["list_notes"])
        self.assertIn("Volcano", call.call_args[0][2])

    def test_small_talk_drops_the_nodes(self):
        with _Server() as s:
            self._configured(s)
            s.galaxy.write_note("volcano.md", "# Volcano\nAbout the volcano.")
            build.build()
            with unittest.mock.patch.object(
                server, "call_claude", return_value=("[[chat]] Hola.", [], None, []),
            ):
                _status, body = s.request("POST", "/chat",
                                          {"question": "hola volcano", "session_id": "t2"})
        self.assertEqual(body["nodes"], [])

    def test_web_answer_keeps_only_its_sources(self):
        sources = [{"title": "A", "url": "https://a.test"}]
        with _Server() as s:
            self._configured(s)
            with unittest.mock.patch.object(
                server, "call_claude", return_value=("[[web]] Según la web.", sources, None, []),
            ):
                _status, body = s.request("POST", "/chat",
                                          {"question": "noticias de hoy", "session_id": "t3"})
        self.assertEqual(body["sources"], sources)

    def test_saved_note_carries_the_new_graph(self):
        with _Server() as s:
            self._configured(s)
            note_result = server.remember("el volcán despierta")
            with unittest.mock.patch.object(
                server, "call_claude",
                return_value=("[[chat]] Guardado.", [], note_result, ["save_note"]),
            ):
                _status, body = s.request("POST", "/chat",
                                          {"question": "recuerda esto", "session_id": "t4"})
        self.assertEqual(body["new_id"], note_result["new_id"])
        self.assertEqual(body["note_title"], note_result["title"])
        self.assertIn("graph", body)

    def test_history_is_reused_and_capped(self):
        with _Server() as s:
            self._configured(s)
            with unittest.mock.patch.object(
                server, "call_claude", return_value=("[[chat]] Ajá.", [], None, []),
            ) as call:
                for i in range(1 + server.MAX_HISTORY_MESSAGES):
                    s.request("POST", "/chat",
                              {"question": "pregunta %d" % i, "session_id": "t5"})
            messages = call.call_args[0][3]
        self.assertEqual(len(messages), server.MAX_HISTORY_MESSAGES + 1)
        self.assertEqual(messages[-1], {"role": "user", "content":
                                       "pregunta %d" % server.MAX_HISTORY_MESSAGES})
        self.assertEqual(len(server._sessions["t5"]), server.MAX_HISTORY_MESSAGES)

    def test_oldest_session_is_evicted_at_capacity(self):
        with _Server() as s:
            self._configured(s)
            with unittest.mock.patch.object(server, "MAX_SESSIONS", 2), \
                 unittest.mock.patch.object(
                     server, "call_claude", return_value=("[[chat]] Ajá.", [], None, []),
                 ):
                for session_id in ("s1", "s2", "s3"):
                    s.request("POST", "/chat", {"question": "hola", "session_id": session_id})
        self.assertEqual(set(server._sessions), {"s2", "s3"})

    def test_marker_only_answer_returns_an_error(self):
        with _Server() as s:
            self._configured(s)
            with unittest.mock.patch.object(
                server, "call_claude", return_value=("[[chat]]", [], None, []),
            ):
                status, body = s.request("POST", "/chat",
                                         {"question": "hola", "session_id": "t6"})
        self.assertEqual(status, 200)
        self.assertIn("ran out of words", body["error"])

    def test_empty_question_returns_400(self):
        with _Server() as s:
            self._configured(s)
            self.assertEqual(s.request("POST", "/chat", {"question": "  "})[0], 400)

    def test_too_long_question_returns_400(self):
        with _Server() as s:
            self._configured(s)
            status, body = s.request(
                "POST", "/chat", {"question": "x" * (server.MAX_QUESTION_CHARS + 1)})
        self.assertEqual(status, 400)
        self.assertIn("too long", body["error"])

    def test_placeholder_api_key_asks_for_a_real_one(self):
        with _Server() as s:
            s.galaxy.write_json(s.galaxy.config_file, {"api_key": "PON-TU-KEY-AQUI"})
            status, body = s.request("POST", "/chat", {"question": "hola"})
        self.assertEqual(status, 200)
        self.assertIn("config.json", body["error"])

    def test_dev_mode_blocks_chat(self):
        with _Server() as s:
            self._configured(s)
            s.request("POST", "/runtime", {"mode": "dev"})
            status, body = s.request("POST", "/chat", {"question": "hola"})
        self.assertEqual(status, 403)
        self.assertIn("Dev mode", body["error"])

    def test_api_failure_is_reported_as_an_error_payload(self):
        with _Server() as s:
            self._configured(s)
            with unittest.mock.patch.object(
                server, "call_claude", side_effect=RuntimeError("Anthropic API error: nope"),
            ):
                status, body = s.request("POST", "/chat",
                                         {"question": "hola", "session_id": "t7"})
        self.assertEqual(status, 200)
        self.assertIn("Anthropic API error", body["error"])


class TestUnexpectedFailures(unittest.TestCase):
    """Every handler wraps its work: an unexpected exception becomes a generic
    500 (never a traceback in the response), and a failed rebuild becomes a
    readable error payload."""

    def setUp(self):
        # the handlers log internal errors to stderr on purpose; keep it quiet
        patcher = unittest.mock.patch.object(sys, "stderr", new_callable=lambda: open(os.devnull, "w"))
        self._stderr = patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._stderr.close)

    def test_get_note_internal_error_is_hidden_behind_a_500(self):
        with _Server() as s:
            with unittest.mock.patch.object(
                server, "_safe_editable_path", side_effect=OSError("disk on fire"),
            ):
                status, body = s.request("GET", "/note?path=docs/x.md")
        self.assertEqual(status, 500)
        self.assertNotIn("disk on fire", json.dumps(body))

    def test_listing_endpoints_report_read_failures(self):
        for path in ("/powers", "/connectors", "/tools"):
            with self.subTest(path=path), _Server() as s:
                with unittest.mock.patch.object(
                    server, "load_json_list", side_effect=RuntimeError("broken json"),
                ):
                    status, body = s.request("GET", path)
                self.assertEqual(status, 500)
                self.assertIn("broken json", body["error"])

    def test_notes_endpoint_reports_search_failures(self):
        with _Server() as s:
            with unittest.mock.patch.object(
                server, "find_notes", side_effect=RuntimeError("no galaxy"),
            ):
                status, body = s.request("GET", "/notes")
        self.assertEqual(status, 500)
        self.assertIn("no galaxy", body["error"])

    def test_post_internal_error_is_hidden_behind_a_500(self):
        with _Server() as s:
            with unittest.mock.patch.object(
                server, "remember", side_effect=ValueError("bug"),
            ):
                status, body = s.request("POST", "/remember", {"text": "hola"})
        self.assertEqual(status, 500)
        self.assertNotIn("bug", json.dumps(body))

    def test_delete_internal_error_is_hidden_behind_a_500(self):
        with _Server() as s:
            with unittest.mock.patch.object(
                server, "_safe_editable_path", side_effect=ValueError("bug"),
            ):
                status, body = s.request("DELETE", "/note?path=docs/x.md")
        self.assertEqual(status, 500)
        self.assertNotIn("bug", json.dumps(body))

    def test_edit_reports_a_failed_rebuild(self):
        with _Server() as s:
            s.galaxy.write_note("note.md", "# Note\nBody.")
            with unittest.mock.patch.object(build, "build", side_effect=Exception("bad docs")):
                status, body = s.request(
                    "POST", "/edit", {"path": "docs/note.md", "content": "# Note\nNew."})
        self.assertEqual(status, 200)
        self.assertIn("Couldn't rebuild the galaxy", body["error"])

    def test_note_delete_reports_a_failed_rebuild(self):
        with _Server() as s:
            s.galaxy.write_note("note.md", "# Note\nBody.")
            with unittest.mock.patch.object(build, "build", side_effect=Exception("bad docs")):
                status, body = s.request("DELETE", "/note?path=docs/note.md")
        self.assertEqual(status, 200)
        self.assertIn("Couldn't rebuild the galaxy", body["error"])


class TestMain(unittest.TestCase):
    def test_main_serves_and_shuts_down_on_interrupt(self):
        fake = unittest.mock.Mock()
        fake.serve_forever.side_effect = KeyboardInterrupt
        with unittest.mock.patch.object(server, "ThreadingHTTPServer", return_value=fake) as ctor:
            server.main()
        self.assertEqual(ctor.call_args[0][0], ("127.0.0.1", server.PORT))
        fake.serve_forever.assert_called_once_with()
        fake.shutdown.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()

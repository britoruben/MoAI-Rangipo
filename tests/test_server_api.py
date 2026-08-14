# -*- coding: utf-8 -*-
"""Offline tests for server.py's outbound API layer: model resolution, the
Anthropic request/tool-use loop, and the ElevenLabs proxy. Every HTTP call
is mocked — nothing leaves the machine."""

import io
import json
import os
import sys
import unittest
import unittest.mock
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server
from tests.galaxy import TempGalaxy


def _response(payload):
    """A urlopen() context manager returning payload as JSON."""
    resp = unittest.mock.MagicMock()
    resp.read.return_value = json.dumps(payload).encode("utf-8")
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


def _http_error(payload):
    return urllib.error.HTTPError(
        "https://example.test", 400, "Bad Request", {},
        io.BytesIO(json.dumps(payload).encode("utf-8")),
    )


def _text(text):
    return {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn"}


def _tool_use(name, tool_input, block_id="tu_1"):
    return {
        "stop_reason": "tool_use",
        "content": [{"type": "tool_use", "id": block_id, "name": name, "input": tool_input}],
    }


class TestResolveModel(unittest.TestCase):
    CONFIG = {"model": "claude-haiku-4-5", "models": [{"id": "claude-sonnet-5"}]}

    def test_allowed_model_is_used(self):
        self.assertEqual(server.resolve_model(self.CONFIG, "claude-sonnet-5"), "claude-sonnet-5")

    def test_default_is_always_allowed(self):
        self.assertEqual(server.resolve_model(self.CONFIG, "claude-haiku-4-5"), "claude-haiku-4-5")

    def test_unknown_model_falls_back_to_default(self):
        self.assertEqual(server.resolve_model(self.CONFIG, "gpt-9"), "claude-haiku-4-5")

    def test_missing_model_key_falls_back_to_builtin_default(self):
        self.assertEqual(server.resolve_model({}, None), "claude-haiku-4-5")


class TestWebSearchTool(unittest.TestCase):
    def test_dynamic_variant_for_capable_models(self):
        tool = server.web_search_tool(sorted(server.WEB_SEARCH_DYNAMIC_MODELS)[0])
        self.assertEqual(tool["type"], "web_search_20260209")

    def test_basic_variant_otherwise(self):
        tool = server.web_search_tool("claude-haiku-4-5")
        self.assertEqual(tool["type"], "web_search_20250305")

    def test_carries_name_and_use_cap(self):
        tool = server.web_search_tool("claude-haiku-4-5")
        self.assertEqual(tool["name"], "web_search")
        self.assertEqual(tool["max_uses"], server.WEB_SEARCH_MAX_USES)


class TestApiRequest(unittest.TestCase):
    CONFIG = {"api_key": "sk-test"}

    def test_sends_key_version_and_tools(self):
        with unittest.mock.patch.object(server.urllib.request, "urlopen",
                                        return_value=_response({"ok": True})) as urlopen:
            result = server._api_request(self.CONFIG, "claude-haiku-4-5", "system",
                                         [{"role": "user", "content": "hola"}])
        self.assertEqual(result, {"ok": True})
        req = urlopen.call_args[0][0]
        self.assertEqual(req.full_url, server.API_URL)
        self.assertEqual(req.get_header("X-api-key"), "sk-test")
        self.assertEqual(req.get_header("Anthropic-version"), server.API_VERSION)
        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(body["system"], "system")
        self.assertIn("save_note", {t.get("name") for t in body["tools"]})

    def test_max_tokens_is_generous_enough_for_search_results(self):
        # web_search_tool_result content counts against max_tokens before
        # any answer text — too low a cap starves the answer itself (see
        # "Moai ran out of words" in call_claude)
        with unittest.mock.patch.object(server.urllib.request, "urlopen",
                                        return_value=_response({"ok": True})) as urlopen:
            server._api_request(self.CONFIG, "claude-haiku-4-5", "system", [])
        body = json.loads(urlopen.call_args[0][0].data.decode("utf-8"))
        self.assertGreaterEqual(body["max_tokens"], 4096)

    def test_http_error_message_is_surfaced(self):
        error = _http_error({"error": {"message": "credit balance too low"}})
        with unittest.mock.patch.object(server.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(RuntimeError) as ctx:
                server._api_request(self.CONFIG, "m", "s", [])
        self.assertIn("credit balance too low", str(ctx.exception))

    def test_unparseable_http_error_still_raises(self):
        error = urllib.error.HTTPError("https://example.test", 500, "Boom", {},
                                      io.BytesIO(b"not json"))
        with unittest.mock.patch.object(server.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(RuntimeError):
                server._api_request(self.CONFIG, "m", "s", [])

    def test_connection_error_is_wrapped(self):
        with unittest.mock.patch.object(server.urllib.request, "urlopen",
                                        side_effect=urllib.error.URLError("offline")):
            with self.assertRaises(RuntimeError) as ctx:
                server._api_request(self.CONFIG, "m", "s", [])
        self.assertIn("offline", str(ctx.exception))


class TestCallClaude(unittest.TestCase):
    CONFIG = {"api_key": "sk-test"}

    def _call(self, responses, messages=None):
        with unittest.mock.patch.object(server, "_api_request",
                                        side_effect=responses) as api:
            result = server.call_claude(
                self.CONFIG, "claude-haiku-4-5", "system",
                messages if messages is not None else [{"role": "user", "content": "hola"}],
            )
        return result, api

    def test_plain_answer_is_normalised(self):
        (answer, sources, note, tools), _api = self._call([_text("  [[chat]]   Hola   Alex. ")])
        self.assertEqual(answer, "[[chat]] Hola Alex.")
        self.assertEqual((sources, note, tools), ([], None, []))

    def test_refusal_short_circuits(self):
        (answer, sources, note, tools), api = self._call([{"stop_reason": "refusal"}])
        self.assertEqual(answer, "No puedo responder a eso.")
        self.assertEqual((sources, note, tools), ([], None, []))
        self.assertEqual(api.call_count, 1)

    def test_empty_answer_raises(self):
        with self.assertRaises(RuntimeError):
            self._call([_text("   ")])

    def test_empty_answer_logs_the_stop_reason(self):
        # the stop_reason is the one clue that distinguishes "search results
        # ate the whole max_tokens budget" from a genuinely empty turn
        response = {"stop_reason": "max_tokens", "content": [
            {"type": "web_search_tool_result", "content": []},
        ]}
        with unittest.mock.patch.object(server, "log_warning") as warn:
            with self.assertRaises(RuntimeError):
                self._call([response])
        self.assertTrue(any("max_tokens" in str(c) for c in warn.call_args_list))

    def test_truncated_but_nonempty_answer_is_still_returned(self):
        # a max_tokens cutoff mid-sentence shouldn't turn a partial-but-real
        # answer into a hard error — it's logged for visibility, not raised
        response = {"stop_reason": "max_tokens", "content": [
            {"type": "text", "text": "[[chat]] Esto se corta a la mit"},
        ]}
        with unittest.mock.patch.object(server, "log_warning") as warn:
            (answer, _sources, _note, _tools), _api = self._call([response])
        self.assertIn("mit", answer)
        self.assertTrue(any("truncated" in str(c) for c in warn.call_args_list))

    def test_tool_loop_limit_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._call([_tool_use("list_connectors", {})] * 4)
        self.assertIn("safe limit", str(ctx.exception))

    def test_web_search_sources_are_deduped_and_scheme_checked(self):
        response = {
            "stop_reason": "end_turn",
            "content": [
                {"type": "text", "text": "[[web]] Two sources."},
                {"type": "web_search_tool_result", "content": [
                    {"url": "https://a.test", "title": "A"},
                    {"url": "https://a.test", "title": "A again"},
                    {"url": "javascript:alert(1)", "title": "Nope"},
                    {"url": "https://b.test"},
                    "not-a-dict",
                ]},
            ],
        }
        (_answer, sources, _note, tools), _api = self._call([response])
        self.assertEqual([s["url"] for s in sources], ["https://a.test", "https://b.test"])
        self.assertEqual(sources[1]["title"], "https://b.test")
        self.assertEqual(tools, ["web_search"])

    def test_save_note_runs_remember_and_feeds_the_result_back(self):
        with TempGalaxy():
            responses = [_tool_use("save_note", {"text": "el volcán despierta"}),
                         _text("[[chat]] Guardado.")]
            (answer, _sources, note, tools), api = self._call(responses)
            self.assertEqual(answer, "[[chat]] Guardado.")
            self.assertEqual(tools, ["save_note"])
            self.assertEqual(note["title"], "El volcán despierta")
        follow_up = api.call_args[0][3]
        self.assertEqual(follow_up[-1]["role"], "user")
        self.assertIn("Note saved", follow_up[-1]["content"][0]["content"])

    def test_failed_save_note_is_reported_to_the_model(self):
        with TempGalaxy():
            responses = [_tool_use("save_note", {"text": "recuerda que"}),
                         _text("[[chat]] No pude.")]
            (_answer, _sources, note, _tools), api = self._call(responses)
            self.assertIsNone(note)
        self.assertIn("Failed to save note",
                      api.call_args[0][3][-1]["content"][0]["content"])

    def test_list_notes_returns_labels(self):
        with TempGalaxy() as g:
            g.write_note("alpha.md", "# Alpha\nContent.")
            server.build.build()
            responses = [_tool_use("list_notes", {}), _text("[[nodes]] Tienes Alpha.")]
            _result, api = self._call(responses)
        self.assertEqual(api.call_args[0][3][-1]["content"][0]["content"], "Alpha (note)")

    def test_list_notes_with_no_match_says_so(self):
        with TempGalaxy():
            responses = [_tool_use("list_notes", {"query": "xyzzyx"}),
                         _text("[[chat]] Nada.")]
            _result, api = self._call(responses)
        self.assertEqual(api.call_args[0][3][-1]["content"][0]["content"],
                         "No matching notes found.")

    def test_list_connectors_and_tools_report_status(self):
        with TempGalaxy() as g:
            g.write_json(g.connectors_file, {"connectors": [
                {"id": "gmail", "label": "Gmail", "status": "placeholder"}]})
            responses = [_tool_use("list_connectors", {}), _text("[[nodes]] Gmail.")]
            _result, api = self._call(responses)
            self.assertEqual(api.call_args[0][3][-1]["content"][0]["content"],
                             "Gmail (placeholder)")

            responses = [_tool_use("list_tools", {}), _text("[[nodes]] Ninguna.")]
            _result, api = self._call(responses)
            self.assertEqual(api.call_args[0][3][-1]["content"][0]["content"],
                             "No tools configured yet.")

    def test_delete_note_removes_the_file(self):
        with TempGalaxy() as g:
            g.write_note("alpha.md", "# Alpha volcano\nContent.")
            server.build.build()
            responses = [_tool_use("delete_note", {"title": "Alpha volcano"}),
                         _text("[[chat]] Borrada.")]
            _result, api = self._call(responses)
            self.assertFalse(os.path.exists(os.path.join(g.docs_dir, "alpha.md")))
        self.assertIn("Deleted note", api.call_args[0][3][-1]["content"][0]["content"])

    def test_delete_note_failure_is_reported(self):
        with TempGalaxy():
            responses = [_tool_use("delete_note", {"title": "ghost"}),
                         _text("[[chat]] No encontrada.")]
            _result, api = self._call(responses)
        self.assertIn("Failed to delete", api.call_args[0][3][-1]["content"][0]["content"])

    def test_undo_delete_restores_the_note_via_the_tool_loop(self):
        with TempGalaxy() as g:
            g.write_note("alpha.md", "# Alpha volcano\nContent.")
            server.build.build()
            responses = [
                _tool_use("delete_note", {"title": "Alpha volcano"}, block_id="tu_1"),
                _tool_use("undo_delete", {}, block_id="tu_2"),
                _text("[[chat]] Deshecho."),
            ]
            _result, api = self._call(responses)
            self.assertTrue(os.path.exists(os.path.join(g.docs_dir, "alpha.md")))
        self.assertIn("Restored", api.call_args[0][3][-1]["content"][0]["content"])

    def test_undo_delete_with_nothing_to_undo_is_reported(self):
        with TempGalaxy():
            responses = [_tool_use("undo_delete", {}), _text("[[chat]] Nada que deshacer.")]
            _result, api = self._call(responses)
        self.assertIn("Failed to undo", api.call_args[0][3][-1]["content"][0]["content"])

    def test_manage_connector_and_manage_tool_write_their_files(self):
        with TempGalaxy() as g:
            responses = [_tool_use("manage_connector",
                                   {"action": "create", "id": "gmail", "label": "Gmail"}),
                         _text("[[chat]] Creado.")]
            _result, api = self._call(responses)
            self.assertIn("Connector 'gmail' created",
                          api.call_args[0][3][-1]["content"][0]["content"])
            self.assertEqual(
                [c["id"] for c in server.load_json_list(g.connectors_file, "connectors")],
                ["gmail"],
            )

            responses = [_tool_use("manage_tool", {"action": "delete", "id": "ghost"}),
                         _text("[[chat]] No existe.")]
            _result, api = self._call(responses)
            self.assertIn("Failed:", api.call_args[0][3][-1]["content"][0]["content"])

    def test_unknown_tool_is_reported_without_crashing(self):
        responses = [_tool_use("teleport", {}), _text("[[chat]] Ni idea.")]
        _result, api = self._call(responses)
        self.assertEqual(api.call_args[0][3][-1]["content"][0]["content"], "Unknown tool.")


class TestTextToSpeech(unittest.TestCase):
    CONFIG = {"api_key": "el-key", "voice_id": "voice/1"}

    def test_posts_text_and_returns_payload(self):
        payload = {"audio_base64": "abc", "alignment": {"characters": []}}
        with unittest.mock.patch.object(server.urllib.request, "urlopen",
                                        return_value=_response(payload)) as urlopen:
            result = server.text_to_speech("hola", self.CONFIG)
        self.assertEqual(result, payload)
        req = urlopen.call_args[0][0]
        self.assertIn("voice%2F1", req.full_url)
        self.assertEqual(req.get_header("Xi-api-key"), "el-key")
        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(body["text"], "hola")
        self.assertEqual(body["model_id"], server.ELEVENLABS_DEFAULT_MODEL)

    def test_configured_model_overrides_the_default(self):
        config = dict(self.CONFIG, model_id="eleven_turbo_v2")
        with unittest.mock.patch.object(server.urllib.request, "urlopen",
                                        return_value=_response({})) as urlopen:
            server.text_to_speech("hola", config)
        body = json.loads(urlopen.call_args[0][0].data.decode("utf-8"))
        self.assertEqual(body["model_id"], "eleven_turbo_v2")

    def test_http_error_detail_is_surfaced(self):
        error = _http_error({"detail": {"message": "quota exceeded"}})
        with unittest.mock.patch.object(server.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(RuntimeError) as ctx:
                server.text_to_speech("hola", self.CONFIG)
        self.assertIn("quota exceeded", str(ctx.exception))

    def test_connection_error_is_wrapped(self):
        with unittest.mock.patch.object(server.urllib.request, "urlopen",
                                        side_effect=urllib.error.URLError("no dns")):
            with self.assertRaises(RuntimeError) as ctx:
                server.text_to_speech("hola", self.CONFIG)
        self.assertIn("no dns", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

"""Tests for the fake provider, run with `python3 -m unittest`."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from typing import Any

import openai_fake_provider as fake

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "skill",
            "description": "Load a skill",
            "parameters": {"type": "object", "properties": {"name": {"type": "string"}}},
        },
    }
]


def chat(*messages: dict[str, Any], model: str = "echo") -> dict[str, Any]:
    return {"model": model, "messages": list(messages), "tools": TOOLS}


def user(text: str) -> dict[str, Any]:
    return {"role": "user", "content": text}


def called(name: str, arguments: str) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": name, "arguments": arguments}}
        ],
    }


def result(text: str) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": "c1", "content": text}


class PlanReplyTest(unittest.TestCase):
    def test_modes(self) -> None:
        request = chat({"role": "system", "content": "sys"}, user("hi"))
        self.assertEqual(fake.plan_reply(request, "ok").text, "ok")
        self.assertIn('"content": "sys"', fake.plan_reply(request, "echo").text)
        self.assertIn("tools (1)", fake.plan_reply(request, "stats").text)
        self.assertIn("## Message 1: user", fake.plan_reply(request, "render").text)

    def test_mode_from_mimicking_model_id(self) -> None:
        self.assertEqual(fake.reply_mode("claude-opus-4-5-stats"), "stats")
        self.assertEqual(fake.reply_mode("gpt-5-codex"), "echo")

    def test_call_directives_play_one_per_turn(self) -> None:
        prompt = user('look\nCALL skill {"name": "a"}\nCALL skill {"name": "b"}')

        first = fake.plan_reply(chat(prompt), "ok")
        self.assertEqual(
            [(c.name, c.arguments) for c in first.tool_calls], [("skill", '{"name":"a"}')]
        )

        second = fake.plan_reply(chat(prompt, called("skill", "{}"), result("A")), "ok")
        self.assertEqual(second.tool_calls[0].arguments, '{"name":"b"}')

        done = chat(prompt, called("skill", "{}"), result("A"), called("skill", "{}"), result("B"))
        self.assertEqual(fake.plan_reply(done, "ok"), fake.Reply(text="ok"))

    def test_call_without_arguments(self) -> None:
        reply = fake.plan_reply(chat(user("CALL todoread")), "ok")
        self.assertEqual(reply.tool_calls[0].arguments, "{}")

    def test_call_with_bad_arguments_explains(self) -> None:
        reply = fake.plan_reply(chat(user("CALL skill {oops")), "ok")
        self.assertFalse(reply.tool_calls)
        self.assertIn("Invalid CALL arguments", reply.text)

    def test_opencode_run_quoting_is_undone(self) -> None:
        reply = fake.plan_reply(chat(user('"CALL skill {\\"name\\": \\"a\\"}"')), "ok")
        self.assertEqual(reply.tool_calls[0].arguments, '{"name":"a"}')

    def test_title_request(self) -> None:
        request = chat(
            {"role": "system", "content": "You are a title generator."},
            user("Generate a title for this conversation:\n"),
            user("CALL skill {}"),
        )
        self.assertEqual(fake.request_kind(request), "title")
        self.assertEqual(fake.plan_reply(request, "ok"), fake.Reply(text="fake: CALL skill {}"))


class AnalyzeTest(unittest.TestCase):
    def test_sections(self) -> None:
        stats = fake.analyze(
            chat({"role": "system", "content": "12345"}, user("abc"), called("skill", "{}"))
        )
        self.assertEqual(stats.system_chars, 5)
        self.assertEqual([m.role for m in stats.messages], ["user", "assistant"])
        self.assertEqual(stats.tools[0].name, "skill")
        self.assertEqual(stats.tools[0].description_chars, len("Load a skill"))
        self.assertEqual(stats.tools_chars, len(fake.compact_json(TOOLS[0])))

    def test_compare_lists_tool_differences(self) -> None:
        a = chat(user("x"))
        b = dict(a, tools=[*TOOLS, {"type": "function", "function": {"name": "extra"}}])
        a["tools"] = []
        report = fake.format_compare(fake.analyze(a), fake.analyze(b), a, b, system_diff=False)
        self.assertIn("Tools only in B (2)", report)
        self.assertIn("extra", report)


class ServerTest(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dump_dir = Path(temp.name)
        self.server = fake.make_server("127.0.0.1", 0, self.dump_dir)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def post(self, body: dict[str, Any]) -> bytes:
        request = urllib.request.Request(
            self.url + "/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request) as response:
            return response.read()

    def test_stream_tool_call_and_dump(self) -> None:
        raw = self.post(dict(chat(user('CALL skill {"name": "a"}')), stream=True)).decode()
        events = [line.removeprefix("data: ") for line in raw.split("\n\n") if line]
        self.assertEqual(events[-1], "[DONE]")
        chunks = [json.loads(e) for e in events[:-1]]
        deltas = [c["choices"][0] for c in chunks if c["choices"]]
        call = deltas[1]["delta"]["tool_calls"][0]
        self.assertEqual(call["function"], {"name": "skill", "arguments": '{"name":"a"}'})
        self.assertEqual(deltas[-1]["finish_reason"], "tool_calls")
        self.assertIn("usage", chunks[-1])
        self.assertEqual([p.name for p in self.dump_dir.iterdir()], ["0001-prompt.json"])

    def test_non_stream_echo(self) -> None:
        body = json.loads(self.post(chat(user("hi"))))
        text = body["choices"][0]["message"]["content"]
        self.assertTrue(text.startswith("```json\n"))
        self.assertEqual(
            json.loads(text.removeprefix("```json\n").removesuffix("\n```")), chat(user("hi"))
        )

    def test_models(self) -> None:
        with urllib.request.urlopen(self.url + "/models") as response:
            ids = [m["id"] for m in json.load(response)["data"]]
        self.assertEqual(ids, list(fake.MODELS))


if __name__ == "__main__":
    unittest.main()

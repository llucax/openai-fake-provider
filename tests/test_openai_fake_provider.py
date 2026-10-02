"""Tests for the fake provider, run with `python3 -m unittest`."""

from __future__ import annotations

import contextlib
import io
import json
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
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


class CliTest(unittest.TestCase):
    def test_serve_does_not_dump_by_default(self) -> None:
        self.assertIsNone(fake.build_parser().parse_args(["serve"]).dump_dir)

    def test_serve_dump_dir(self) -> None:
        args = fake.build_parser().parse_args(["serve", "--dump-dir", "x"])
        self.assertEqual(args.dump_dir, Path("x"))

    def test_serve_idle_exit(self) -> None:
        self.assertIsNone(fake.build_parser().parse_args(["serve"]).idle_exit)
        args = fake.build_parser().parse_args(["serve", "--idle-exit", "1.5"])
        self.assertEqual(args.idle_exit, 1.5)
        for invalid in ("0", "-1", "nan", "inf"):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                fake.build_parser().parse_args(["serve", "--idle-exit", invalid])


class IdleExitTest(unittest.TestCase):
    def test_exits_only_after_idle(self) -> None:
        server = fake.make_server("127.0.0.1", 0, None)
        self.addCleanup(server.server_close)
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        url = f"http://127.0.0.1:{server.server_address[1]}/v1/models"
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            fake.exit_when_idle(server, 0.6)
            for _ in range(3):  # 0.9 s in all, but never 0.6 s idle
                time.sleep(0.3)
                urllib.request.urlopen(url).close()
            # A kept-alive connection waiting for its next request is idle.
            idle_client = socket.create_connection(server.server_address)
            self.addCleanup(idle_client.close)
            self.assertTrue(serving.is_alive())
            serving.join(timeout=3)
        self.assertFalse(serving.is_alive())
        self.assertIn("no requests for 0.01 minutes, exiting", stderr.getvalue())

    def test_request_in_progress_is_not_idle(self) -> None:
        server = fake.make_server("127.0.0.1", 0, None)
        self.addCleanup(server.server_close)
        with server.activity():
            time.sleep(0.05)
            self.assertEqual(server.idle_seconds(), 0)
        self.assertLess(server.idle_seconds(), 0.05)


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

    def post(self, body: dict[str, Any], headers: dict[str, str] | None = None) -> bytes:
        request = urllib.request.Request(
            self.url + "/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        with urllib.request.urlopen(request) as response:
            return response.read()

    def answer(self, body: dict[str, Any], headers: dict[str, str] | None = None) -> str:
        return json.loads(self.post(body, headers))["choices"][0]["message"]["content"]

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

    def test_dump_dir_per_request(self) -> None:
        with tempfile.TemporaryDirectory() as other:
            header = {fake.DUMP_DIR_HEADER: other}
            self.post(chat(user("a"), model="ok"))
            self.post(chat(user("b"), model="ok"), header)
            text = self.answer(chat(user("c"), model="stats"), header)

            self.assertEqual(sorted(p.name for p in self.dump_dir.iterdir()), ["0001-prompt.json"])
            self.assertEqual(
                sorted(p.name for p in Path(other).iterdir()),
                ["0001-prompt.json", "0002-prompt.json"],
            )
            self.assertTrue(text.endswith(f"\n\nsaved as {Path(other) / '0002-prompt.json'}"))

    def test_dump_dir_header_naming_the_default(self) -> None:
        self.post(chat(user("a"), model="ok"))
        self.post(chat(user("b"), model="ok"), {fake.DUMP_DIR_HEADER: str(self.dump_dir)})
        self.post(chat(user("c"), model="ok"))
        self.assertEqual(
            sorted(p.name for p in self.dump_dir.iterdir()),
            ["0001-prompt.json", "0002-prompt.json", "0003-prompt.json"],
        )

    def test_dump_dir_must_be_absolute(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post(chat(user("a")), {fake.DUMP_DIR_HEADER: "relative"})
        self.assertEqual(caught.exception.code, 400)
        self.assertIn("absolute path", caught.exception.read().decode())

    def test_stats_without_dump_has_no_footer(self) -> None:
        server = fake.make_server("127.0.0.1", 0, None)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.url = f"http://127.0.0.1:{server.server_address[1]}/v1"
        self.assertNotIn("saved as", self.answer(chat(user("a"), model="stats")))

    def test_models(self) -> None:
        with urllib.request.urlopen(self.url + "/models") as response:
            ids = [m["id"] for m in json.load(response)["data"]]
            server = response.headers["Server"]
        self.assertEqual(ids, list(fake.MODELS))
        self.assertTrue(server.startswith(f"openai-fake-provider/{fake.__version__} "))

    def test_serve_on_a_used_port(self) -> None:
        port = str(self.server.server_address[1])
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(fake.main(["serve", "--port", port]), 1)
        self.assertEqual(stderr.getvalue(), f"127.0.0.1:{port} is already in use\n")


if __name__ == "__main__":
    unittest.main()

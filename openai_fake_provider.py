#!/usr/bin/env python3
"""Fake OpenAI-compatible LLM provider for inspecting what clients send.

It serves the OpenAI Chat Completions API (streaming and not), saves every
request body it receives to disk, and answers without calling any real model.
The model ID picks the answer:

- `echo`: the full request body as pretty-printed JSON.
- `render`: the request rendered as readable text.
- `stats`: a size breakdown of system messages, tools and messages.
- `ok`: the fixed text "ok".

A line `CALL <tool> <json-args>` in the last user message makes it answer with
that tool call instead. Several `CALL` lines are played one per turn, and once
the last tool result comes back the model's normal answer follows, so the
request that carries the tool result can be inspected too.

Only the standard library is used, so the file runs anywhere with Python 3.11+.
"""

from __future__ import annotations

import argparse
import dataclasses
import difflib
import json
import re
import sys
import threading
import time
import uuid
from collections.abc import Iterator, Sequence
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4141
DEFAULT_PROVIDER_ID = "fake"

MODELS = {
    "echo": "Echo (full request JSON)",
    "render": "Render (request as readable text)",
    "stats": "Stats (request size breakdown)",
    "ok": "OK (fixed short reply)",
}
"""Model IDs served, with the display name used in the opencode config."""

CALL_RE = re.compile(r"^[ \t]*CALL[ \t]+(?P<name>\S+)[ \t]*(?P<args>.*?)[ \t]*$", re.MULTILINE)
"""A tool call directive, one per line of the last user message."""

TITLE_MARKER = "Generate a title for this conversation"
"""How opencode starts the user message of its title generation request."""

CHARS_PER_TOKEN = 4
"""Rough ratio used for token estimates; real tokenizers differ by model."""

STREAM_CHUNK_CHARS = 2048


# Request inspection


def content_text(content: Any) -> str:
    """Return the text of a message `content` field, whatever its shape.

    Non-text parts (images, files) are kept as their JSON so their size still
    counts.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part.get("text", "")
            if isinstance(part, dict) and part.get("type") == "text"
            else compact_json(part)
            for part in content
        )
    return compact_json(content)


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def message_text(message: dict[str, Any]) -> str:
    """Return everything a message contributes to the context, as text."""
    text = content_text(message.get("content"))
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        text += f"\n{function.get('name', '')} {function.get('arguments', '')}"
    return text


def estimate_tokens(chars: int) -> int:
    return round(chars / CHARS_PER_TOKEN)


def last_user_index(messages: Sequence[dict[str, Any]]) -> int | None:
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") == "user":
            return index
    return None


def is_title_request(messages: Sequence[dict[str, Any]]) -> bool:
    return any(
        message.get("role") == "user"
        and content_text(message.get("content")).lstrip().startswith(TITLE_MARKER)
        for message in messages
    )


def request_kind(request: dict[str, Any]) -> str:
    """Classify a request for dump file names and logs."""
    messages = request.get("messages") or []
    if is_title_request(messages):
        return "title"
    if messages and messages[-1].get("role") == "tool":
        return "tool-result"
    return "prompt"


@dataclasses.dataclass(frozen=True)
class ToolStats:
    name: str
    description_chars: int
    parameters_chars: int
    total_chars: int
    """Size of the whole compact JSON definition sent to the model."""


@dataclasses.dataclass(frozen=True)
class MessageStats:
    index: int
    role: str
    chars: int
    preview: str


@dataclasses.dataclass(frozen=True)
class RequestStats:
    model: str
    body_chars: int
    system: list[MessageStats]
    tools: list[ToolStats]
    messages: list[MessageStats]

    @property
    def system_chars(self) -> int:
        return sum(m.chars for m in self.system)

    @property
    def tools_chars(self) -> int:
        return sum(t.total_chars for t in self.tools)

    @property
    def messages_chars(self) -> int:
        return sum(m.chars for m in self.messages)

    @property
    def total_chars(self) -> int:
        return self.system_chars + self.tools_chars + self.messages_chars


def analyze(request: dict[str, Any]) -> RequestStats:
    """Measure how much of a chat completions request each part takes."""
    system: list[MessageStats] = []
    messages: list[MessageStats] = []
    for index, message in enumerate(request.get("messages") or []):
        role = message.get("role", "?")
        text = message_text(message)
        stats = MessageStats(index, role, len(text), preview(text))
        (system if role in ("system", "developer") else messages).append(stats)

    tools = []
    for tool in request.get("tools") or []:
        function = tool.get("function", tool)
        tools.append(
            ToolStats(
                name=function.get("name", "?"),
                description_chars=len(function.get("description") or ""),
                parameters_chars=len(compact_json(function.get("parameters") or {})),
                total_chars=len(compact_json(tool)),
            )
        )

    return RequestStats(
        model=str(request.get("model", "?")),
        body_chars=len(compact_json(request)),
        system=system,
        tools=tools,
        messages=messages,
    )


def preview(text: str, width: int = 60) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[: width - 3] + "..."


# Report formatting


def format_stats(stats: RequestStats) -> str:
    total = stats.total_chars or 1
    lines = [
        (
            f"model {stats.model}: {len(stats.system)} system messages, "
            f"{len(stats.tools)} tools, {len(stats.messages)} other messages"
        ),
        f"~tok assumes {CHARS_PER_TOKEN} chars per token, a rough estimate.",
        "",
        f"{'section':<24} {'chars':>9} {'~tok':>8} {'share':>6}",
    ]
    for name, chars in (
        (f"system ({len(stats.system)})", stats.system_chars),
        (f"tools ({len(stats.tools)})", stats.tools_chars),
        (f"messages ({len(stats.messages)})", stats.messages_chars),
    ):
        lines.append(f"{name:<24} {chars:>9,} {estimate_tokens(chars):>8,} {chars / total:>6.1%}")
    lines.append(f"{'total':<24} {stats.total_chars:>9,} {estimate_tokens(stats.total_chars):>8,}")
    lines.append(f"{'(raw request JSON)':<24} {stats.body_chars:>9,}")

    if stats.system:
        lines += ["", "System messages", f"  {'#':>3} {'chars':>9} {'~tok':>8}  start"]
        lines += [
            f"  {m.index:>3} {m.chars:>9,} {estimate_tokens(m.chars):>8,}  {m.preview}"
            for m in stats.system
        ]

    if stats.tools:
        width = max(len(t.name) for t in stats.tools)
        lines += [
            "",
            "Tools, largest first",
            f"  {'name':<{width}} {'desc':>7} {'params':>7} {'total':>8} {'~tok':>7}",
        ]
        lines += [
            f"  {t.name:<{width}} {t.description_chars:>7,} {t.parameters_chars:>7,}"
            f" {t.total_chars:>8,} {estimate_tokens(t.total_chars):>7,}"
            for t in sorted(stats.tools, key=lambda t: t.total_chars, reverse=True)
        ]

    if stats.messages:
        lines += ["", "Messages", f"  {'#':>3} {'role':<10} {'chars':>9} {'~tok':>8}  start"]
        lines += [
            f"  {m.index:>3} {m.role:<10} {m.chars:>9,} {estimate_tokens(m.chars):>8,}  {m.preview}"
            for m in stats.messages
        ]
    return "\n".join(lines)


def format_compare(
    a: RequestStats,
    b: RequestStats,
    a_request: dict[str, Any],
    b_request: dict[str, Any],
    *,
    system_diff: bool,
) -> str:
    lines = [
        f"{'section':<12} {'A chars':>9} {'B chars':>9} {'delta':>9} {'~tok delta':>11}",
    ]
    for name, a_chars, b_chars in (
        ("system", a.system_chars, b.system_chars),
        ("tools", a.tools_chars, b.tools_chars),
        ("messages", a.messages_chars, b.messages_chars),
        ("total", a.total_chars, b.total_chars),
    ):
        delta = b_chars - a_chars
        lines.append(
            f"{name:<12} {a_chars:>9,} {b_chars:>9,} {delta:>+9,} {estimate_tokens(delta):>+11,}"
        )

    a_tools = {t.name: t for t in a.tools}
    b_tools = {t.name: t for t in b.tools}
    added = sorted(b_tools.keys() - a_tools.keys(), key=lambda n: -b_tools[n].total_chars)
    removed = sorted(a_tools.keys() - b_tools.keys(), key=lambda n: -a_tools[n].total_chars)
    changed = sorted(
        (
            n
            for n in a_tools.keys() & b_tools.keys()
            if a_tools[n].total_chars != b_tools[n].total_chars
        ),
        key=lambda n: -abs(b_tools[n].total_chars - a_tools[n].total_chars),
    )
    for title, names, sign, source in (
        ("Tools only in B", added, "+", b_tools),
        ("Tools only in A", removed, "-", a_tools),
    ):
        if names:
            lines += ["", f"{title} ({len(names)})"]
            lines += [f"  {sign}{source[n].total_chars:>8,} chars  {n}" for n in names]
    if changed:
        lines += ["", f"Tools that changed size ({len(changed)})"]
        lines += [
            f"  {b_tools[n].total_chars - a_tools[n].total_chars:>+9,} chars  {n}" for n in changed
        ]

    if system_diff:
        diff = difflib.unified_diff(
            system_text(a_request).splitlines(),
            system_text(b_request).splitlines(),
            "A system",
            "B system",
            lineterm="",
        )
        lines += ["", *diff]
    return "\n".join(lines)


def system_text(request: dict[str, Any]) -> str:
    return "\n".join(
        content_text(m.get("content"))
        for m in request.get("messages") or []
        if m.get("role") in ("system", "developer")
    )


def render(request: dict[str, Any]) -> str:
    """Render a request as plain text, for reading rather than parsing."""
    out = [f"# Request for model {request.get('model', '?')}"]
    for index, message in enumerate(request.get("messages") or []):
        role = message.get("role", "?")
        text = content_text(message.get("content"))
        out += ["", f"## Message {index}: {role} ({len(text):,} chars)", "", text]
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            out += [
                "",
                (
                    f"[tool call {call.get('id', '')}] "
                    f"{function.get('name', '')} {function.get('arguments', '')}"
                ),
            ]
    tools = request.get("tools") or []
    out += ["", f"# Tools ({len(tools)})"]
    for tool in tools:
        function = tool.get("function", tool)
        out += [
            "",
            f"## {function.get('name', '?')} ({len(compact_json(tool)):,} chars)",
            "",
            function.get("description") or "(no description)",
            "",
            "Parameters: " + compact_json(function.get("parameters") or {}),
        ]
    return "\n".join(out)


# Answer planning


@dataclasses.dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str
    """The arguments as a JSON string, as the API sends them."""


@dataclasses.dataclass(frozen=True)
class Reply:
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()


def plan_reply(request: dict[str, Any], model: str) -> Reply:
    """Decide what the fake model answers to a request."""
    messages = request.get("messages") or []
    if is_title_request(messages):
        return Reply(text=title_for(messages))

    user_index = last_user_index(messages)
    if user_index is not None:
        text = unquote(content_text(messages[user_index].get("content")))
        directives = list(CALL_RE.finditer(text))
        done = sum(
            1
            for m in messages[user_index + 1 :]
            if m.get("role") == "assistant" and m.get("tool_calls")
        )
        if done < len(directives):
            return call_reply(directives[done])

    return Reply(text=answer_text(request, model))


def unquote(text: str) -> str:
    """Undo the quoting `opencode run` adds to arguments containing spaces.

    `opencode run 'CALL skill {"name": "x"}'` sends `"CALL skill {\\"name\\": \\"x\\"}"`,
    which would otherwise hide the directive.
    """
    stripped = text.strip()
    if len(stripped) < 2 or stripped[0] != '"' or stripped[-1] != '"':
        return text
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        return text
    return value if isinstance(value, str) else text


def title_for(messages: Sequence[dict[str, Any]]) -> str:
    """Make a recognizable session title from the user's first prompt."""
    for message in reversed(messages):
        text = (
            unquote(content_text(message.get("content"))) if message.get("role") == "user" else ""
        )
        if text and not text.lstrip().startswith(TITLE_MARKER):
            return "fake: " + preview(text, 50)
    return "fake provider session"


def call_reply(directive: re.Match[str]) -> Reply:
    raw = directive.group("args") or "{}"
    try:
        arguments = json.loads(raw)
    except json.JSONDecodeError as error:
        return Reply(text=f"Invalid CALL arguments {raw!r}: {error}")
    if not isinstance(arguments, dict):
        return Reply(text=f"CALL arguments must be a JSON object, got {raw!r}")
    call = ToolCall(
        id=f"call_{uuid.uuid4().hex[:12]}",
        name=directive.group("name"),
        arguments=compact_json(arguments),
    )
    return Reply(tool_calls=(call,))


def reply_mode(model: str) -> str:
    """Pick the reply mode from the last mode word in the model ID.

    Matching a word rather than the whole ID lets the ID also carry a name the
    client reacts to, as in `claude-opus-4-5-stats`; unknown IDs get `echo`.
    """
    words = re.split(r"[^a-z0-9]+", model.lower())
    return next((word for word in reversed(words) if word in MODELS), "echo")


def answer_text(request: dict[str, Any], model: str) -> str:
    match reply_mode(model):
        case "ok":
            return "ok"
        case "stats":
            return "```\n" + format_stats(analyze(request)) + "\n```"
        case "render":
            return render(request)
        case _:
            pretty = json.dumps(request, indent=2, ensure_ascii=False)
            return "```json\n" + pretty + "\n```"


# HTTP server


class Dumper:
    """Write each request body to a numbered JSON file."""

    def __init__(self, directory: Path | None) -> None:
        self._directory = directory
        self._lock = threading.Lock()
        self._next = 1
        if directory is not None:
            directory.mkdir(parents=True, exist_ok=True)
            numbers = [
                int(p.name.split("-", 1)[0])
                for p in directory.glob("[0-9]*-*.json")
                if p.name.split("-", 1)[0].isdigit()
            ]
            self._next = max(numbers, default=0) + 1

    def dump(self, request: dict[str, Any], kind: str) -> tuple[int, Path | None]:
        with self._lock:
            number = self._next
            self._next += 1
        if self._directory is None:
            return number, None
        path = self._directory / f"{number:04d}-{kind}.json"
        path.write_text(json.dumps(request, indent=2, ensure_ascii=False) + "\n")
        return number, path


class Handler(BaseHTTPRequestHandler):
    server_version = "openai-fake-provider"
    protocol_version = "HTTP/1.1"
    dumper: Dumper

    def log_message(self, format: str, *args: Any) -> None:
        pass  # Requests are logged by handle_chat, in a more useful form.

    def do_GET(self) -> None:
        if self.path.rstrip("/") in ("/v1/models", "/models"):
            self.send_json(
                {
                    "object": "list",
                    "data": [
                        {"id": model, "object": "model", "created": 0, "owned_by": "fake"}
                        for model in MODELS
                    ],
                }
            )
            return
        self.send_error_json(HTTPStatus.NOT_FOUND, f"no route for GET {self.path}")

    def do_POST(self) -> None:
        if self.path.rstrip("/") not in ("/v1/chat/completions", "/chat/completions"):
            self.send_error_json(HTTPStatus.NOT_FOUND, f"no route for POST {self.path}")
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            request = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as error:
            self.send_error_json(HTTPStatus.BAD_REQUEST, f"invalid JSON body: {error}")
            return
        self.handle_chat(request)

    def handle_chat(self, request: dict[str, Any]) -> None:
        model = str(request.get("model", "echo"))
        kind = request_kind(request)
        number, path = self.dumper.dump(request, kind)
        stats = analyze(request)
        reply = plan_reply(request, model)
        outcome = (
            "call " + ", ".join(f"{c.name} {c.arguments}" for c in reply.tool_calls)
            if reply.tool_calls
            else f"text {len(reply.text):,} chars"
        )
        print(
            f"#{number:04d} {kind:<11} model={model} system={stats.system_chars:,} "
            f"tools={len(stats.tools)}/{stats.tools_chars:,} "
            f"messages={len(stats.messages)}/{stats.messages_chars:,} "
            f"~{estimate_tokens(stats.total_chars):,} tok -> {outcome}"
            + (f" [{path}]" if path else ""),
            file=sys.stderr,
            flush=True,
        )

        usage = {
            "prompt_tokens": estimate_tokens(stats.total_chars),
            "completion_tokens": estimate_tokens(
                len(reply.text) + sum(len(c.arguments) for c in reply.tool_calls)
            ),
        }
        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
        completion_id = f"chatcmpl-fake-{number:04d}"
        if request.get("stream"):
            self.send_stream(stream_chunks(reply, model, completion_id, usage))
        else:
            self.send_json(completion(reply, model, completion_id, usage))

    def send_json(self, body: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_error_json(self, status: HTTPStatus, message: str) -> None:
        self.send_json({"error": {"message": message, "type": "invalid_request_error"}}, status)

    def send_stream(self, chunks: Iterator[dict[str, Any]]) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            for chunk in chunks:
                self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            print("client disconnected mid-stream", file=sys.stderr)


def stream_chunks(
    reply: Reply, model: str, completion_id: str, usage: dict[str, int]
) -> Iterator[dict[str, Any]]:
    base = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
    }

    def chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
        return {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

    yield chunk({"role": "assistant", "content": ""})
    for start in range(0, len(reply.text), STREAM_CHUNK_CHARS):
        yield chunk({"content": reply.text[start : start + STREAM_CHUNK_CHARS]})
    for index, call in enumerate(reply.tool_calls):
        yield chunk(
            {
                "tool_calls": [
                    {
                        "index": index,
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": call.arguments},
                    }
                ]
            }
        )
    yield chunk({}, "tool_calls" if reply.tool_calls else "stop")
    yield {**base, "choices": [], "usage": usage}


def completion(
    reply: Reply, model: str, completion_id: str, usage: dict[str, int]
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": reply.text or None}
    if reply.tool_calls:
        message["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments},
            }
            for call in reply.tool_calls
        ]
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if reply.tool_calls else "stop",
            }
        ],
        "usage": usage,
    }


def make_server(host: str, port: int, dump_dir: Path | None) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"dumper": Dumper(dump_dir)})
    return ThreadingHTTPServer((host, port), handler)


# opencode integration


def opencode_config(base_url: str, provider_id: str, mimic: str | None = None) -> dict[str, Any]:
    """Build an opencode config that registers the fake provider.

    `small_model` points at the fake provider too, so title generation does
    not send the conversation to a real model configured elsewhere.

    opencode picks the base system prompt from the model ID it sends, so a
    `mimic` model name such as `claude-opus-4-5` is prepended to each ID to get
    the prompt that model family would get. Model keys stay `echo`, `stats`
    and so on either way.
    """
    return {
        "$schema": "https://opencode.ai/config.json",
        "model": f"{provider_id}/echo",
        "small_model": f"{provider_id}/ok",
        "provider": {
            provider_id: {
                "name": "Fake (openai-fake-provider)",
                "npm": "@ai-sdk/openai-compatible",
                "options": {"baseURL": base_url, "apiKey": "fake"},
                "models": {
                    model: {
                        "id": f"{mimic}-{model}" if mimic else model,
                        "name": f"{name}, as {mimic}" if mimic else name,
                        "tool_call": True,
                        "attachment": False,
                        "reasoning": False,
                        "temperature": False,
                        "limit": {"context": 1_000_000, "output": 100_000},
                        "cost": {"input": 0, "output": 0},
                    }
                    for model, name in MODELS.items()
                },
            }
        },
    }


# Command line


def load_request(path: str) -> dict[str, Any]:
    text = sys.stdin.read() if path == "-" else Path(path).read_text()
    return json.loads(text)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fake OpenAI-compatible LLM provider for inspecting what clients send."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="run the fake provider")
    serve.add_argument("--host", default=DEFAULT_HOST)
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve.add_argument(
        "--dump-dir",
        type=Path,
        metavar="DIR",
        help="save request bodies to DIR (default: not saved, since they contain full "
        "prompts, which are private)",
    )

    stats = commands.add_parser("stats", help="size breakdown of a saved request")
    stats.add_argument("file", help="request JSON file, or - for stdin")

    compare = commands.add_parser("compare", help="compare the sizes of two saved requests")
    compare.add_argument("a")
    compare.add_argument("b")
    compare.add_argument(
        "--system-diff", action="store_true", help="also show a diff of the system messages"
    )

    render_cmd = commands.add_parser("render", help="print a saved request as readable text")
    render_cmd.add_argument("file", help="request JSON file, or - for stdin")

    config = commands.add_parser(
        "opencode-config", help="print an opencode config registering the fake provider"
    )
    config.add_argument("--host", default=DEFAULT_HOST)
    config.add_argument("--port", type=int, default=DEFAULT_PORT)
    config.add_argument("--provider-id", default=DEFAULT_PROVIDER_ID)
    config.add_argument(
        "--mimic",
        metavar="MODEL",
        help="prefix model IDs with this name (e.g. claude-opus-4-5) so opencode "
        "uses the system prompt it would use for that model",
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    match args.command:
        case "serve":
            dump_dir = args.dump_dir
            server = make_server(args.host, args.port, dump_dir)
            host, port = server.server_address[:2]
            print(
                f"serving http://{host}:{port}/v1, models: {', '.join(MODELS)}"
                + (
                    f", saving requests to {dump_dir}/"
                    if dump_dir
                    else ", not saving requests (use --dump-dir to save them)"
                ),
                file=sys.stderr,
                flush=True,
            )
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
        case "stats":
            print(format_stats(analyze(load_request(args.file))))
        case "compare":
            a, b = load_request(args.a), load_request(args.b)
            print(format_compare(analyze(a), analyze(b), a, b, system_diff=args.system_diff))
        case "render":
            print(render(load_request(args.file)))
        case "opencode-config":
            base_url = f"http://{args.host}:{args.port}/v1"
            print(json.dumps(opencode_config(base_url, args.provider_id, args.mimic), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

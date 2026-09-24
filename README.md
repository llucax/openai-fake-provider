# openai-fake-provider

A fake LLM provider that speaks the OpenAI Chat Completions API, saves every
request it receives and answers without calling any real model. It is meant for
seeing exactly what a client sends: how big the system prompt is, which tools
it declares and how much each one costs, and how a tool result changes the
next request. The main target is [opencode](https://opencode.ai), to measure how
much context plugins, MCP servers, skills and instructions add, but any client
that can point the OpenAI API at a custom base URL works.

It is a single Python file with no dependencies, needing Python 3.11 or newer.

## Models

The model ID picks the answer:

| Model    | Answer                                                          |
| -------- | --------------------------------------------------------------- |
| `echo`   | The full request body as pretty-printed JSON                    |
| `render` | The request as readable text: system messages, messages, tools  |
| `stats`  | A size breakdown of system messages, tools (each) and messages  |
| `ok`     | The fixed text `ok`, for when only the saved request matters    |

The mode is the last of these words found in the ID, so `claude-opus-4-5-stats`
answers like `stats`. Any other ID answers like `echo`.

Title generation requests from opencode are detected and answered with
`fake: <your prompt>`, so the fake sessions are easy to spot.

## Tool calls

A line `CALL <tool> <json-arguments>` in the last user message makes the model
call that tool instead of answering. The arguments default to `{}`.

```text
CALL skill {"name": "github"}
```

With several `CALL` lines, one call is made per turn, in order. After the last
tool result comes back the model gives its normal answer, so with `stats` the
final answer is the breakdown of the request that carries all the tool results.

## Saved requests

Each request body is written to `requests/NNNN-<kind>.json` (change the
directory with `--dump-dir`, or disable it with `--no-dump`). The kind is
`prompt` when the last message is from the user, `tool-result` when it is a
tool result, and `title` for opencode's title generation. Numbering continues
across restarts. The server also logs a one-line summary of each request to
stderr.

Three commands work on saved requests:

- `stats FILE` prints the same breakdown as the `stats` model.
- `render FILE` prints the request as readable text.
- `compare A B [--system-diff]` shows the size difference per section, the
  tools only present in one of them or whose size changed, and optionally a
  diff of the system messages.

Sizes are counted in characters. Token counts marked `~tok` assume 4 characters
per token, a rough estimate that real tokenizers can be quite far from.

## Using it with opencode

Start the server, which listens on `127.0.0.1:4141` by default:

```sh
./openai_fake_provider.py serve
```

In another terminal, run opencode with the config printed by `opencode-config`
merged into your own. It adds a provider `fake` with the four models and sets
both `model` and `small_model` to it, so no request reaches a real model by
accident:

```sh
export OPENCODE_CONFIG_CONTENT="$(./openai_fake_provider.py opencode-config)"
opencode run -m fake/stats "hello"
opencode run -m fake/stats 'CALL skill {"name": "github"}'
opencode -m fake/echo     # the TUI works too
```

To measure what something adds, save a request with and without it and compare
them:

```sh
./openai_fake_provider.py compare requests/0002-prompt.json requests/0005-prompt.json
```

### Things to keep in mind

- opencode picks its base system prompt from the model ID, so with the plain
  IDs it uses its default prompt, not the one a Claude or GPT model gets. Pass
  `--mimic <model>` to `opencode-config` (for example `--mimic claude-opus-4-5`)
  to send IDs like `claude-opus-4-5-stats` and get that model's prompt. The
  system prompt also mentions the model ID, which changes its size slightly.
- The request is what opencode sends through `@ai-sdk/openai-compatible`.
  Native providers such as Anthropic's lay it out differently (separate system
  blocks, cache markers), but the text inside is the same.
- `opencode run` wraps an argument containing spaces in quotes. The server
  undoes that when looking for `CALL` lines, but piping the prompt through
  stdin avoids it altogether.
- Fake sessions are real opencode sessions and show up in the session list.
  Setting `XDG_DATA_HOME` to a scratch directory keeps them, and everything
  else opencode stores, out of your real data. Otherwise remove them with
  `opencode session delete <id>`.

## Installing

It runs straight from a checkout. To get an `openai-fake-provider` command
instead:

```sh
uv tool install .
```

## Development

```sh
python3 -m unittest
```

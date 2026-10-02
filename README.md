# openai-fake-provider

A fake LLM provider that speaks the OpenAI Chat Completions API, answers without
calling any real model and can save every request it receives. It is meant for
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

Requests are not saved by default, because they contain the full prompts,
which are private. Pass `--dump-dir DIR` to `serve` and each request body is
written to `DIR/NNNN-<kind>.json`. A temporary directory keeps them from
piling up somewhere they could be shared by accident:

```sh
./openai_fake_provider.py serve --dump-dir "$(mktemp -d)"
```

The kind is
`prompt` when the last message is from the user, `tool-result` when it is a
tool result, and `title` for opencode's title generation. Numbering continues
across restarts that reuse the same directory. The server also logs a one-line summary of each request to
stderr.

A client can also pick the directory per request, with an
`X-Fake-Provider-Dump-Dir` header holding an absolute path. That request is
saved there instead, whatever `--dump-dir` says, and each directory is
numbered on its own. The header is only accepted from loopback clients. The
`stats` model's answer ends with `saved as <path>` whenever the request was
saved, so the caller knows which file to compare later.

Three commands work on saved requests:

- `stats FILE` prints the same breakdown as the `stats` model.
- `render FILE` prints the request as readable text.
- `compare A B [--system-diff]` shows the size difference per section, the
  tools only present in one of them or whose size changed, and optionally a
  diff of the system messages.

Sizes are counted in characters. Token counts marked `~tok` assume 4 characters
per token, a rough estimate that real tokenizers can be quite far from.

## Using it with opencode

The [opencode plugin](#the-opencode-plugin) below does all of this for you.
By hand, start the server, which listens on `127.0.0.1:4141` by default. The models
answer without saving anything, so only pass `--dump-dir` when you want the
requests on disk, for example to compare them later:

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
them. Start the server with a temporary dump directory, then compare two of the
files in it:

```sh
dump=$(mktemp -d)
./openai_fake_provider.py serve --dump-dir "$dump"
# ...run opencode twice, then:
./openai_fake_provider.py compare "$dump/0002-prompt.json" "$dump/0005-prompt.json"
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

## The opencode plugin

`opencode/plugin.ts` makes all of the above automatic. It adds the `fake`
provider to opencode's config, starts the server right before the first
request that goes to it, and stops the server again when opencode exits. There
is no need to start anything by hand or to set `OPENCODE_CONFIG_CONTENT`, and
unlike that config it never touches `model` or `small_model`, so your real
sessions keep their models.

### Installing it

Clone the repository and add the plugin to the `plugin` array of your
opencode config. Relative paths are resolved against the config file, so from
`~/.config/opencode/opencode.jsonc` with the clone in `~/opencode-plugins/`:

```jsonc
{
  "plugin": [
    ["../../opencode-plugins/openai-fake-provider/opencode/plugin.ts", { "idleExit": 30 }]
  ]
}
```

The second element holds the options, and can be left out. The plugin needs
no `npm install`: it only uses Node's built-in modules and Python 3.11 or newer
to run the server. It works as a file symlinked into opencode's `plugins/`
directory too, but opencode gives such plugins no options, so only the
defaults and the environment variables apply. Don't install it both ways, or
it loads twice.

Check it with `opencode models fake`, which lists the four models, and
`opencode run -m fake/ok hello`, which answers `ok`.

### Options

| Option       | Default     | Meaning                                          |
| ------------ | ----------- | ------------------------------------------------ |
| `autostart`  | `on-use`    | when to start the server                         |
| `match`      | `baseURL`   | how a request is recognized as fake              |
| `providerID` | `fake`      | the provider's ID in opencode                    |
| `host`       | `127.0.0.1` | where the provider points                        |
| `port`       | `4141`      | where the provider points                        |
| `mimic`      | none        | model ID prefix, as `opencode-config --mimic`    |
| `idleExit`   | `60`        | minutes without requests before the server exits |
| `dumpDir`    | none        | directory to save each fake request to           |
| `logFile`    | see below   | the server's log                                 |
| `python`     | `python3`   | the interpreter that runs the server             |

Every option can also be set with an environment variable, which wins over
the config: `OPENAI_FAKE_PROVIDER_` followed by the option name in upper
snake case, as in `OPENAI_FAKE_PROVIDER_AUTOSTART`,
`OPENAI_FAKE_PROVIDER_PROVIDER_ID` or `OPENAI_FAKE_PROVIDER_DUMP_DIR`. An
empty variable counts as unset. An unknown option or an invalid value makes
opencode report the plugin as failed to load, with the reason.

`autostart` is `on-use`, `on-start` or `off`. With `on-use` the server starts
right before the first request to the fake provider, which costs about 0.3 s
once. `on-start` starts it as soon as opencode loads its config, for when
something other than opencode should find it already running. `off` never
starts it; the provider is still added.

`match` is `baseURL` or `model`. With `baseURL`, a request is fake when it goes
to the host and port the provider points to, so it still works if you add a
second provider pointing to the same server, for example with different
`mimic` IDs. With `model`, it is fake when its model is `<providerID>/...`.

`host` and `port` only set where the added provider points. The plugin
manages whatever server the provider ends up pointing to, and only on a
loopback address; it never starts a server anywhere else.

`dumpDir` must be an absolute path, or start with `~/`. The plugin sends it
with each fake request as the `X-Fake-Provider-Dump-Dir` header (see "Saved
requests"), so it works with a server that someone else started, too. With a
fixed directory every fake session's prompts pile up there, so the environment
variable for one command is usually the better fit:

```sh
OPENAI_FAKE_PROVIDER_DUMP_DIR="$(mktemp -d)" opencode run -m fake/stats hello
```

The answer ends with the path the request was saved as. opencode's title
requests are saved too, as `NNNN-title.json`.

### Overriding the provider

The provider comes from the script's `opencode-config` output. To change
parts of it, write a provider block with the same ID in your config: the
plugin merges it over its own, recursively, and your fields win. For example,
to rename a model and point the provider to another port:

```jsonc
{
  "provider": {
    "fake": {
      "options": { "baseURL": "http://127.0.0.1:4242/v1" },
      "models": { "stats": { "name": "Context size" } }
    }
  }
}
```

Everything you leave out keeps the plugin's value, and the plugin then starts
the server on port 4242. Models can be added this way, but not removed.

### When the server stops

One server is shared by every opencode process on the machine. The process
that started it stops it when it exits normally, and the next fake request
from any other process starts it again. A request in flight at that exact
moment can fail, which is unlikely since the fake models answer at once.
When opencode is killed instead, nobody stops the server, so it stops itself
after `idleExit` minutes without requests. Set `idleExit` to 0 to keep it
running forever.

The plugin never restarts a server that is already running, so after
updating the repository, stop the old one so the next request starts the new
code:

```sh
pkill -f 'openai_fake_provider.py serve'
```

### The server's log

The server's output goes to `$TMPDIR/openai-fake-provider/server-<port>.log`
(`/tmp/...` when `TMPDIR` isn't set), or to `logFile`, as an absolute path.
It's truncated every time the plugin starts the server, since it's only
useful right after a request, for example to check that a request that got
no answer reached the server at all. When the server fails to start, the
error quotes the log's last lines. If something other than the fake provider
already listens on the port, the fake request fails with an error saying so.

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

The opencode plugin is type-checked and tested with Node 24, which runs
TypeScript directly:

```sh
cd opencode
npm ci
npm run check
```

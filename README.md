# simple-agent

**An agent that writes its own tools, hires its own specialists, and edits its own system prompt.**

One file. One class. About 740 lines. No framework.

```
> reverse the word harness

  ...notices it has no string tools
  ...writes reverse_string, registers it, runs it in a subprocess

The reversed form of "harness" is ssenrah
```

The tool is still there tomorrow. And the day after. That is the whole idea.

---

## Why this exists

Every agent framework ships you a toolbox. Someone else decided what is in it.
You get a `search_web`, a `read_file`, a `run_sql`, and when your actual problem
needs something slightly different, you go write a plugin, register it, restart,
and hope the schema lines up.

This one ships almost nothing. A handful of meta-tools: levers that let it change
itself. Everything else it builds, on demand, while working on your problem, and
keeps.

The bet underneath it is simple. **A model that can write Python can write its own
tools better than you can guess them in advance.** Your job stops being "anticipate
every capability" and becomes "correct it when it is wrong." That is a much
smaller job, and one you are far better at.

What that buys you:

- **It gets better at your work specifically.** Not better at benchmarks. Better at
  the shape of the things you actually ask for, because the tools it keeps are the
  ones your questions demanded.
- **It does not bloat.** As it grows tools, it promotes them into specialists, and
  those tools leave its prompt. A hundred tools does not mean a hundred-tool
  context. It stays fast while it gets deep.
- **You can read all of it.** One file. Open it, follow the loop, change the parts
  you disagree with. There is no plugin lifecycle, no dependency injection, no
  four layers of abstraction between you and the model call.
- **It is honest about failure.** Every tool call is recorded with its outcome. When
  something silently does not work, the record shows it, and the agent reads its
  own record.
- **It separates code from judgment.** Deterministic work becomes a tool. Repeated
  judgment becomes a skill. Most agents only have one of those, so everything
  ends up crammed into whichever one they have.

---

## The split

The design has exactly one rule, and every decision falls out of it:

| The harness owns | The model owns |
|---|---|
| Growth mechanics (draft → validate → register) | *What* to build |
| Sandboxed execution | *When* to promote a tool family into a specialist |
| Secrets custody | *When* to dissolve one |
| Raw storage | *What* to remember, and in what format |
| Registries for tools, skills, team | *What its own system prompt should say* |
| Trace log with outcomes | *When code is right and when prose is right* |

Anything with judgment in it belongs to the model. Anything mechanical, and
anything that must not be talked out of its job — the sandbox, the vault —
belongs to the harness.

The harness never decides *what*. The model never touches *how*.

---

## Quickstart

```bash
git clone <this repo> && cd simple-agent
./setup.sh                    # venv + 2 dependencies + .env
$EDITOR .env                  # paste one API key
python3 agent.py
```

That is the whole install. Two dependencies, both pure Python:
`arcllm` (the LLM client) and `python-dotenv`. No vendor SDK, no framework,
no build step. `setup.sh` uses [uv](https://github.com/astral-sh/uv) if you
have it and falls back to `venv` + `pip` if you do not.

Any one key in `.env` is enough. Anthropic is the default; `--provider openai`
switches. A local Ollama needs no key at all.

```
[cold start]
[anthropic · claude-sonnet-5]
[!secret NAME | !secrets | !fb <text> | tools | skills | skill <name> | team |
 identity | raw | history | cost | exit]

>
```

`python3 agent.py` works from any shell, including one that has never heard of
arcllm: if the interpreter that started it cannot import arcllm but `.venv` can,
`agent.py` re-execs itself there. Only when it is the entrypoint, so
`import agent` from another program is never hijacked.

---

## The levers

| Meta-tool | What the model does with it |
|---|---|
| `grow_tool` | Drafts a new Python method from a description. Callable on the very next step. |
| `grow_skill` | Writes durable instructions to itself, in prose, for judgment code cannot hold. |
| `read_skill` | Loads one skill's full text, on demand. |
| `forget_skill` | Deletes one that no longer holds. |
| `update_identity` | Replaces its own system prompt, entirely. |
| `create_specialist` | Promotes a family of tools and skills into a standing expert. |
| `call_specialist` | Routes a task to one. |
| `dissolve_specialist` | Retires one that has gone stale. |
| `spawn_agent` | One-off sub-agent: assembled, run, gone. |

Everything else in its context is something it built.

---

## Two kinds of growth

This is the part most agent designs get wrong by only having one.

**Tools are code.** Deterministic. Same input, same output, every time. Parsing,
math, storage, formatting, API calls. Determinism is bedrock: never do in prose
what code can do exactly.

**Skills are prose.** Durable instructions the agent writes to itself for the
part code cannot decide. A procedure worth repeating. House style. Domain rules.
A checklist. A gotcha that cost it a mistake once.

```
> When I ask you to size a pump, always confirm flow rate, head, and fluid
  type before quoting, and never quote without a lead time. Also I need
  percent-change math on numbers. Set yourself up for both.

  ok  grow_skill(pump_sizing_quote_procedure)
  ok  grow_tool(percent_change)
```

It split them itself. The procedure could not be code. The math should never be
prose.

**Skills are cheap to carry.** Only the name and a one-line *when to use it* sit
in the prompt. The body loads through `read_skill` when the moment arrives. Two
skills cost about 190 characters of context no matter how long their bodies get.
A library of fifty is still an index.

**They compound.** The agent is told to write a skill the moment it catches
itself re-deriving the same judgment, and to rewrite one whenever it learns
something that would have made it better. The maintenance pass sees how often
each skill was actually read, and is asked to sharpen the vague ones and forget
the dead ones. Skills that never get read are visible; skills that carry the work
get refined.

```
> skills                      # the index, with read counts
> skill vendor_risk_review    # the full text
```

---

## How growth works

```
model calls grow_tool("look up a company by name, fuzzy match")
        ↓
harness asks the model for {"name": ..., "code": "def ..."}
        ↓
compile() + "def <name>(" present?          ← rejected here if not
        ↓
manifest["lookup_company"] = code
        ↓
next turn: it is in the tool schema, and callable
```

Grown code gets full Python. Any import, `open()` inside its workspace, network
if you passed `--allow-network`. It runs in a **subprocess**, not in the agent's
process, with a 30 second timeout and a scrubbed environment.

> **Sandbox honesty:** subprocess isolation stops *accidents* — hangs, crashes, a
> tool casually reading the vault file. It does not stop malice: same user, same
> filesystem. Container-wrap the identical runner before real stakes.

---

## The team model

A mixture-of-experts that the agent assembles itself, built on one rule: **the
root's context shrinks as the team grows.**

```
                    ┌──────────────────────────────┐
                    │      ONE central registry    │   ← single source of truth
                    │  every tool AND skill lives  │
                    └──────────────┬───────────────┘
                                   │
              ┌────────────────────┼────────────────────┐
              │                    │                    │
       ┌──────▼──────┐      ┌──────▼──────┐     ┌───────▼───────┐
       │ root context│      │ specialist  │     │  specialist   │
       │ unassigned  │      │ 4 tools      │     │  6 tools      │
       │ only        │      │ 2 skills    │     │  5 skills     │
       └─────────────┘      │ own memory  │     │  own memory   │
                            └─────────────┘     └───────────────┘
```

- A specialist is a **persistent named view** over the registry: an identity, a
  tool subset, a skill subset, and its own memory file that survives between
  calls. That memory is what tunes over months.
- Whatever a specialist owns **leaves the root's context** — tools drop out of
  the schema, skills drop out of the index. That is the overload relief.
- Between calls a specialist is just a registry entry. Nothing runs idle.
- Anything a specialist grows or sharpens merges back into the central registry
  under its ownership. No cross-specialist drift.
- Specialists tune their own prompts too, and the tuned version persists.

`spawn_agent` is the opposite: a one-off worker for a subtask. Its growth is
*reported*, not kept.

**One catch worth knowing:** a specialist can only be built from tools and skills
that already exist. Ask for a specialist on day one and it has nothing to
promote. Give it a few real tasks in that domain first, let it grow the pieces,
then promote. Or just say so directly: *"grow the tools and skills for X, then
promote them into a specialist."*

---

## Memory

One raw file per agent. No schema imposed.

```python
self._raw_read()            # -> str
self._raw_write(content)    # str -> None
```

That is the entire interface. The model decides the format — JSON, prose,
line-delimited records — and builds `save_fact`, `search_notes`, whatever it
needs on top with `grow_tool`, then improves them as it learns what actually
works.

This is deliberate. A format the harness picks is a format the model is stuck
with. A format the model picks is one it can outgrow.

---

## Secrets

Values never pass through chat.

```
> !secret STRIPE_KEY
value for STRIPE_KEY (hidden, local-only): ••••••••
[stored STRIPE_KEY — grown code reads it via self._secret('STRIPE_KEY')]
```

- Stored in `agent_workspace/.secrets.json`, mode `0600`.
- Injected into the sandbox as `SECRET_*` env vars. The model never sees a value.
- Every sandbox result is scanned on the way out. A leaked value comes back as
  `[REDACTED:STRIPE_KEY]`.
- One vault, harness-owned, shared by every specialist and sub-agent.

This is what makes "go call that API for me" safe to say out loud.

---

## How it learns

Three mechanisms, in increasing order of force.

**1. Traces.** Every tool call is recorded: name, arguments, whether it worked,
and the first 200 characters of what came back. Harness failures all start with
`FAILED: `, so success is unambiguous and nothing has to be guessed at. Type
`history` to see what the agent sees.

**2. Maintenance.** Every 25 tasks it runs itself through a review, and it is
handed a digest of its own recent history: which tools it leans on, which have
never been called, every call that failed and why, every correction you gave it.
Then it merges redundant tools, repairs what broke, promotes families that have
reached critical mass, dissolves specialists gone stale, and rewrites its
identity to match reality.

**3. Correction.** The blunt instrument.

```
> !fb you have a company lookup tool. use it before saying you have no record.
```

This marks the last task failed, records your words as ground truth, and runs one
reconciliation turn where the agent must fix *whatever produced the error* —
stored memory, a tool, a specialist, or its own prompt — so it stays fixed. Not
apologize. Fix.

One sentence from you becomes a permanent change in the system. That is the
whole loop, and it is the highest-leverage thing in this repo.

---

## Ideas: what to have it build for itself

The agent starts empty on purpose. Here is what a first week can look like.
Every line below is something you can literally type at the prompt.

**Give it a memory worth having**

```
> build yourself a way to save facts about people and companies I mention,
  and a way to search them by partial name
> now add a way to list everything you know about one subject at once
```

Within three turns it has a personal CRM it designed itself, in whatever format
it decided was right.

**Point it at your files**

```
> grow a tool that reads any CSV or JSON in ./workdir and describes its columns
> now one that answers questions about a file by loading it and filtering
```

It becomes a data analyst for exactly your file shapes, not a generic one.

**Let it reach the network** (`--allow-network`)

```
> !secret GITHUB_TOKEN
> build a tool that lists my open pull requests, then one that summarizes
  what changed in each
```

It writes its own API client. No SDK, no wrapper library, no integration to
maintain.

**Give it a routine**

```
> every time I paste a meeting note, extract the commitments and who owns them,
  and save them. build whatever you need for that.
> what am I on the hook for this week?
```

**Teach it how you want things done**

```
> write yourself a skill for how I like meeting notes summarized: decisions
  first, owners named, no filler
> when you learn something new about how I work, update that skill
```

Skills are where your preferences live. Tools are where your determinism lives.

**Then promote**

```
> those note tools and skills have piled up. promote them into a specialist
  and give it a name.
```

Now that whole domain has its own prompt and its own memory, and it is out of
the root agent's context.

**Other directions people take it**

- **Inbox triage** — classify, draft replies, remember who always needs a nudge
- **Codebase janitor** — find dead code, stale TODOs, drifted docs, in your idioms
- **Reading pile** — save articles, extract claims, connect them to old notes
- **Money** — parse statements, categorize, notice when a subscription changes
- **On-call helper** — remember which alerts were noise last time and why
- **Job hunt / deal pipeline** — companies, contacts, stage, what was said
- **Learning tutor** — track what you have covered, remember what you got wrong
- **Home lab** — poll your own services, remember normal, tell you about weird

The pattern is always the same. Tell it what you want in plain language. Let it
build the tool. Correct it once when it is wrong. It stays corrected.

---

## Why arcllm

LLM calls go through [arcllm](https://github.com/joshuamschultz/Arc), a
provider-agnostic client that never imports a vendor SDK. Every call is direct
HTTP you can read.

```bash
python3 agent.py --provider openai
python3 agent.py --provider google --model gemini-2.5-pro
python3 agent.py --provider ollama        # fully local, air-gapped
```

Not one line of `agent.py` changes. The entire LLM layer is two methods:

```python
def _client(self):                     # long-lived, one connection pool
    return load_model(self.provider, self.model, telemetry=True, retry=True)

def _invoke(self, messages, tools):    # arcllm is async; the agent is not
    resp = _sync(self._client().invoke(messages, tools, max_tokens=2000))
    self.cost_usd += resp.cost_usd or 0.0
    return resp
```

What comes with it, for free:

- **Cost tracking.** Per call, rolled up from specialists and sub-agents into the
  parent. Type `cost`.
- **Retry.** Exponential backoff on 429s and 5xx, on by default.
- **One response shape.** `content`, `tool_calls`, `stop_reason`, normalized across
  every provider. The agent loop never sees a wire format.
- **Opt-in modules.** PII redaction, request signing, audit events,
  OpenTelemetry, rate limiting. Flip them on at `load_model()`. The agent code
  is untouched.

Measured overhead versus raw `httpx` to the same endpoint: none.

### One gotcha worth stealing

`agent.py` loads its `.env` with `override=True`, so the project's key beats
whatever your shell exports. This was not paranoia. A shell exporting an *OpenAI*
key under the name `ANTHROPIC_API_KEY` made every Anthropic call 401, and a
configured fallback chain quietly answered with a different provider. Correct
answers, wrong model, no error anywhere. A local `.env` that wins removes that
entire class of bug.

---

## Configuration

| Flag | Default | Effect |
|---|---|---|
| `--provider` | `anthropic` | Any arcllm provider |
| `--model` | provider default | e.g. `claude-opus-4-6` |
| `--workspace` | `agent_workspace` | Memory, secrets, team dirs, trace log |
| `--state` | `agent_state.json` | Tools, skills, identity, team, recent traces |
| `--allow-network` | off | Grown code may make network calls |
| `--allow-spawn` | off | Enables `spawn_agent` (max depth 2) |

Tuning constants sit at the top of `agent.py`: `MAX_TOOL_ITERS`,
`MAX_SPAWN_DEPTH`, `MAINTENANCE_EVERY`, `SANDBOX_TIMEOUT`, `TRACES_IN_STATE`.

**Running for months.** Full history is appended to
`agent_workspace/traces.jsonl`. Only the last 200 traces ride in
`agent_state.json`, so the state file stays roughly fixed size no matter how long
the agent has been alive. What actually grows is the tool manifest and the skill
library — and specialists keep both out of the prompt, while skill bodies were
never in it to begin with.

---

## Where the seams are

Stated plainly, because an agent that edits itself deserves an honest README.

- **The sandbox stops accidents, not attackers.** Same user, same filesystem.
- **A grown tool is only as good as its draft.** Validation is `compile()` plus a
  signature check. It catches broken code, not wrong code. Wrong code gets caught
  by `!fb`, one task later. That is the design, not an oversight, but you should
  know which one you are relying on.
- **Identity is fully replaceable, including by mistake.** `update_identity` is a
  replacement, not an append. Prompt drift over hundreds of tasks is real. The
  maintenance pass is the countermeasure, not a guarantee.
- **A confident wrong answer still looks confident.** The agent will sometimes say
  it has recorded something it did not record. The trace log is how you catch it,
  and `!fb` is how you fix it. Check `history` when something feels off.
- **State is one JSON file.** No migrations, no locking. Fine for one agent on one
  machine, which is exactly what this is.

---

## License

MIT. Take it apart.

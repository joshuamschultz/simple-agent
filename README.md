<p align="center">
  <img src="assets/header.svg" alt="simple-agent — one class that builds its own harness while you use it" width="100%">
</p>

<p align="center">
  <a href="#quickstart"><img alt="setup" src="https://img.shields.io/badge/setup-one_command-4f9cf9"></a>
  <img alt="dependencies" src="https://img.shields.io/badge/dependencies-2-7c5cff">
  <img alt="python" src="https://img.shields.io/badge/python-3.11%2B-3776ab">
  <img alt="license" src="https://img.shields.io/badge/license-MIT-63d19e">
</p>

<p align="center"><b>An assistant that builds its own abilities as you use it.<br>
The more you work with it, the better it gets at <i>your</i> work.</b></p>

---

## What this actually is

Most AI assistants come with a fixed set of abilities someone else chose. When
your real work needs something a little different, you're stuck waiting for a
plugin, an integration, or a new release.

This one starts with almost nothing on purpose. When it hits something it can't
do, **it writes the ability itself, right then, and keeps it.**

You talk to it in plain English. You never write code.

### Watch it happen

You ask something it has never been asked before:

```
> how did Q3 close out?
```

Instead of guessing, it builds what it needs:

```
  ✎ built a tool     parse_ledger            reads a ledger into monthly totals
  ▸ ran it           parse_ledger(q3.csv)    →  jul 41,200 · aug 38,650 · sep …
  ✎ wrote a note     margin_review           when summarizing a quarter
  ✓ answered

Q3 closed at $132,160, up 14% on Q2. September carried it — roughly
half the quarter landed in the last five weeks.
```

Those lines are real output, printed live as it works. You watch it build the
thing, then use it.

Two things now exist that didn't sixty seconds ago:

- **A tool.** Real working code that reads your ledger. It runs safely, in its own
  sandbox, and it's there forever.
- **A note to itself.** How *you* like quarters summarized. Judgment, not code.

Ask next quarter and it just answers. That's the whole idea.

---

## The loop

<p align="center">
  <img src="assets/loop.svg" width="330" alt="You ask. If it can't already, it builds the ability. It answers. Then it checks whether that was wrong or due for review, and fixes the cause.">
</p>

---

## Quickstart

Three commands and one paste. About a minute.

```bash
git clone https://github.com/joshuamschultz/simple-agent && cd simple-agent
./setup.sh
```

Open the `.env` file it created, paste in an API key, save. Then:

```bash
python3 agent.py
```

```
[cold start]
[anthropic · claude-sonnet-5]
> _
```

That's it. **Two dependencies.** No accounts to create, no services to configure,
nothing running in the background. Everything stays on your machine.

<details>
<summary><b>Which API key do I need?</b></summary>

<br>

Any one of them. The default is Anthropic — get a key at
[console.anthropic.com](https://console.anthropic.com). Paste it into `.env` next
to `ANTHROPIC_API_KEY=`.

Prefer something else? `python3 agent.py --provider openai` and paste an OpenAI
key instead. Running a local model with Ollama needs no key at all.

You pay the model provider directly for what you use. Type `cost` at any time to
see what this session has spent.
</details>

<details>
<summary><b>Is my data going anywhere?</b></summary>

<br>

Only what you type goes to the model provider you chose, same as any chat
assistant. Everything the agent learns — its tools, its notes, your facts — is
stored in plain files in this folder. Nothing is uploaded anywhere else, and
there's no account or telemetry.

Passwords and API keys you give it are stored separately with locked-down file
permissions, handed to running code as environment variables, and scrubbed out of
anything it says back to you. It never sees the values itself.
</details>

---

## The three things it builds

Most assistants only have one of these. Having all three is what makes it
compound.

| | 🔧 **Tools** | 📘 **Notes to itself** | 🗂 **Facts** |
|---|---|---|---|
| **What it is** | Working code | Standing instructions | What's true in your world |
| **Good for** | Anything exact and repeatable | Judgment and taste | Names, numbers, decisions |
| **Example** | reads your ledger file | *"lead with the total, always give a lead time"* | *"Acme sells us steel — fast but pricey"* |

Say all three at once and it sorts them out itself:

```
> When I ask you to price a job, always confirm the quantity and the deadline
  first, and never quote without a lead time. I also need percent-change math.
  And remember our main supplier is Acme.

  ✎ wrote a note     pricing_procedure       ← taste
  ✎ built a tool     percent_change          ← exact math
  ▸ ran it           save_fact(supplier,…)   ← something true
```

**It stays fast as it grows.** Only a one-line summary of each ability sits in
front of it. The details load only when it actually reaches for one — so fifty
abilities cost about as much as five.

---

## It corrects itself

Two ways, and neither one asks you to touch code.

### 1. Just tell it when it's wrong

```
> !fb you already have a supplier lookup — use it before saying you don't know
```

One sentence. It marks that answer as wrong, treats your words as the final say,
and has to go fix **whatever actually caused it** — the tool, the note, the saved
data, or its own instructions. Not apologize. Fix.

### 2. It re-reads its own work

Every few tasks it goes back over what you asked, what its tools returned, and
what it told you, with one question in mind: **did you get what you wanted?**
Something can technically work and still be wrong.

Here's a real thing it caught on its own:

> *"My supplier lookup and my customer lookup were both saving names to the same
> place, so 'Acme Corp' was showing up as both a supplier and a customer. I
> rewrote both to keep their records separate, made lookups forgiving about
> capitals so 'acme' finds 'Acme Corp', and wrote myself a note so this can't
> happen again."*

Nobody told it. It found that by reading its own history.

<p align="center">
  <img src="assets/review.svg" width="560" alt="It reads its own history and asks: did you get what you wanted? Then it fixes a tool, writes a note, changes how it builds, or repairs saved data.">
</p>

---

## It brings on specialists

Once one area of your work builds up enough abilities, it promotes them into a
**specialist** — a focused expert with its own instructions and its own memory.

Those abilities then move *out* of the main assistant's head. So the more it
learns, the more focused it stays.

<p align="center">
  <img src="assets/specialists.svg" width="600" alt="Everything it has built sits in one shared store. The main assistant hands matching work off to a specialist, which has its own instructions, its own tools and notes pared off the main assistant, and its own memory file kept between calls.">
</p>

A new specialist gets **four things of its own**:

| | |
|---|---|
| **Its own instructions** | A starting prompt written for that corner of the work, not the general one. It rewrites its own prompt as it learns. |
| **Its own tools** | A named handful of what's been built so far, handed over to it. |
| **Its own notes** | The skills that go with those tools, handed over too. |
| **Its own memory file** | A folder on disk it keeps updating, call after call. This is the part that tunes over months. |

The tools and notes it takes are **pared off the main assistant** — it stops
carrying them, which is the whole point. There's still only one shared store
underneath, so nothing gets duplicated or drifts out of sync. And anything the
specialist builds while working goes straight back into that shared store, under
its name.

Specialists don't run in the background or cost anything when idle. They're just
there when a matching task comes up, remembering everything they've learned about
that corner of your work.

---

## What people point it at

Same assistant, different jobs. Every line is paste-ready.

**Email**
```
> draft my replies: decision first, short, no filler. save that as how you
  always write email for me
```

**Morning briefing**
```
> each morning, give me one screen: what's open, what's late, what I said
  I'd do. most urgent first
```

**Meeting notes**
```
> here are my notes. pull the decisions, who owns each one, and the dates.
  keep a running list I can ask about later
```

**Customer and vendor notes**
```
> remember what I tell you about each customer and supplier, and show me
  the history next time I mention one
```

**Planning**
```
> turn this project into weeks with dependencies, and tell me what slips
  if one task moves
```

**Sales planning and coaching**
```
> track my pipeline by stage and value. every monday, tell me which deals
  went quiet and what to say to each one
```

**SEO and AEO**
```
> read this page and tell me two things: what it's missing to rank, and
  what it's missing to get quoted in an AI answer
```

**Bookkeeping and admin**
```
> categorize these expenses the way I did last month, and flag anything
  that doesn't match a pattern you've seen
```

Nothing above is a feature that shipped with it. It builds each one the first
time you ask, and keeps it.

---

## Things to try on day one

Every line here is something you can type as-is.

**Give it a memory**
```
> keep track of the people and companies I mention, and let me look them
  up later even if I only remember part of the name
```

**Point it at your files**
```
> look at the spreadsheets in this folder and tell me what's in them
> now let me ask questions about any of them
```

**Teach it your taste**
```
> when you write anything for me, lead with the answer, then the detail.
  no preamble, no summary at the end.
```

**Let it reach the web** (start with `python3 agent.py --allow-network`)
```
> !secret GITHUB_TOKEN
> show me my open pull requests and summarize what changed in each
```

**Let it tidy up**
```
> those note-taking abilities have piled up — turn them into a specialist
```

The pattern never changes. **Say what you want. Let it build. Correct it once.**

---

## Watching it work

It narrates itself while it works, so you can see it building and using its own
machinery instead of guessing.

| Line | What just happened |
|---|---|
| `✎ built a tool` | it wrote working code and kept it |
| `✎ wrote a note` | it wrote a standing instruction to itself |
| `▸ ran it` | it used something it built, and what came back |
| `↳ read a note` | it loaded one of its notes *before* doing the work |
| `⟲ rewrote itself` | it changed its own instructions |
| `⚑ new specialist` | it promoted a group of abilities into an expert |
| `→ handed off to` | a specialist took the task — its own lines indent underneath |
| `✗ failed` | a call didn't work. It sees this too, and has to deal with it |
| `·· reviewing its own work ··` | the periodic self-review, happening right now |

Start with `python3 agent.py --quiet` if you'd rather just see answers.

---

## While you're at the prompt

| Type this | To see |
|---|---|
| `tools` / `skills` | what it has built so far |
| `skill <name>` | one of its notes, in full |
| `team` | its specialists |
| `identity` | its current instructions |
| `history` | this session, the way it reviews it |
| `review` | make it review itself right now |
| `raw` | everything it has remembered |
| `cost` | what you've spent this session |
| `!fb <text>` | correct it — this is the important one |
| `!secret NAME` | store a password or key, typed privately |
| `new` | start a fresh conversation, keep everything it learned |

---

<details>
<summary><h2 style="display:inline">For the technically curious</h2></summary>

<br>

**Three files, three jobs.**

```
agent.py     the harness — knows nothing about any model provider
llm.py       the seam — one method, plain dicts. Swap it for litellm, raw HTTP, anything
prompts.py   the words — seeded into state on first run, then the agent owns them
```

**One JSON file is the whole agent.** `agent_state.json` — tools, notes,
prompts, identity, specialists, recent history. No database, no migration.

```jsonc
{
  "manifest": { "manage_contacts": { "code": "def manage_contacts(self, action: str, ...", "description": "..." } },
  "skills":   { "pricing_procedure": { "when": "when asked to price a job", "body": "...", "uses": 3 } },
  "prompts":  { "identity": "...", "draft": "...", "review": "..." },
  "team":     { "sales": { "identity": "...", "tools": [...], "skills": [...], "description": "..." } },
  "traces":   [ ... ]
}
```

**Everything is a string — including the code.** A grown tool is not a module
and not a plugin. It is the *text* of a Python function, sitting in JSON, and it
gets turned back into a callable on demand:

- Every turn, `_tools_schema()` `exec`s each stored code string, reads the
  resulting function with `inspect.signature`, and builds the tool schema from
  that. Nothing is imported. Nothing is precompiled.
- Every turn, `_build_system()` rebuilds the system prompt from
  `prompts["identity"]`, plus a one-line index of every skill, plus the team
  roster.
- To actually run a tool, `_run_sandboxed()` joins every code string together,
  drops the lot into a runner template, and `exec`s it in a subprocess where
  each function is bound onto a shim object. That binding is why grown code can
  call `self._raw_read()` and `self._secret("NAME")`.

So there's no build step and no registration. The model writes a string; on the
next turn the string is a tool.

**You can edit the same file it edits.** Quit, open `agent_state.json`, change
anything — reword its identity, fix a line inside a grown tool, delete a skill,
retune the `draft` prompt that writes every future tool — then start it again.
It reloads straight from that JSON and your change is live on the next message.
It saves after every task, so edit between runs rather than during one. Delete
the file to go back to stock; new keys added to `prompts.py` flow into an
existing state on its next load.

**Grown code runs in a subprocess** with a timeout and a scrubbed environment.

> That stops *accidents* — hangs, crashes, a tool wandering somewhere it
> shouldn't. It does not stop malice: same user, same filesystem. Container-wrap
> the runner before real stakes.

**What it can change about itself**

| Lever | What it does |
|---|---|
| `grow_tool` / `read_tool` | write a new tool, or read one back to repair it |
| `grow_skill` / `read_skill` / `forget_skill` | write, load, or drop a standing instruction |
| `update_identity` | rewrite its own system prompt |
| `read_prompt` / `update_prompt` | rewrite how it drafts tools, or how it reviews itself |
| `create_specialist` / `call_specialist` / `dissolve_specialist` | manage its team |
| `spawn_agent` | one-off sub-agent, then gone |

**The activity lines are a hook, not a `print`.** Set `agent.on_event = fn` and
you get a dict per tool call — `{phase, tool, args, result, ok, meta, depth,
fresh}` — twice per call (`start`, then `end`), plus once per turn when an
answer lands. Specialists and sub-agents inherit the same callback and report
their own `depth`, which is what makes their work indent. `_reporter()` in
`agent.py` is just one consumer; swap in a logger, a UI, a metrics sink.
Exceptions inside an observer are swallowed on purpose — a broken display must
never cost you a task.

**How memory actually gets made**

Three kinds of memory, made three different ways.

*A fact* — "our main supplier is Acme"

1. The model looks for a tool that owns that kind of record. If there isn't one,
   it calls `grow_tool` first, then saves.
2. It calls that tool. The tool runs in a subprocess.
3. Inside, `self._raw_read()` hands back the **entire** store as one JSON
   string. The tool parses it, puts its record under its own top-level key, and
   writes the whole document back with `self._raw_write(json.dumps(data))`.
4. That write is atomic — temp file, then rename. A crash leaves the previous
   contents intact.

One file, `agent_workspace/memory.data`, shared by every tool it has ever
written. Read the whole thing, change only your own key, write the whole thing
back. That rule lives in the `draft` prompt, so every tool it writes is told it.

*A note to itself* — "always confirm quantity and deadline"

`grow_skill(name, when_to_use, body)` writes straight into `state["skills"]`.
Only `name` and `when_to_use` ride in the system prompt; `read_skill` pulls the
body in on the turn it's needed. That's why a large library stays cheap.

*A tool*

`grow_tool(description)` makes a **second** model call using the `draft` prompt.
That call returns JSON — `{name, description, code}`. The harness `compile()`s
the code to check it parses and confirms `def <name>(` is really in there. That
is the whole check: valid, not correct. Then it lands in `state["manifest"]` and
is callable on the next step. Growing under an existing name *replaces* it,
which is how it repairs its own tools instead of piling up near-duplicates.

*History*

Every task appends one line to `agent_workspace/traces.jsonl` — what you asked,
each tool call with its result, what it answered. The most recent 200 also ride
in the state file. The review reads that transcript back and asks one question
of it.

*What deliberately doesn't persist:* the conversation. Restart and the thread is
clean. The tools, skills, facts and identity are not.

**What a specialist actually is**

`create_specialist(name, identity, tools, description, skills)` writes one entry
into `state["team"]` — a seed prompt, a list of tool names, a list of skill
names, a one-line roster description — and makes the folder
`agent_workspace/team/<name>/`. That's the whole registration. Nothing is
copied, nothing runs.

`call_specialist(name, task)` then assembles it on the spot:

- a fresh `SelfBuildingAgent` pointed at that folder, so it gets its **own**
  `memory.data` and its own `traces.jsonl`
- `child.manifest` = only the named tools; `child.skills` = only the named
  skills — subsets of the one central registry, not copies of it
- `child.identity` = the seed prompt from its team entry
- the root's connection pool and the single harness-owned secrets vault, shared

It runs one task, then everything it grew merges back: new tools and skills join
the central registry and get appended to its own lists, and if it rewrote its
own prompt, that replaces the seed in `state["team"]`. Next call starts from the
sharper version.

Two things follow from the subsets. `_tools_schema()` and `_build_system()` skip
anything a specialist owns, which is how the root's context *shrinks* as the
team grows. And a specialist runs on a reduced lever set (`_META_CHILD`): it can
grow tools, grow skills and rewrite its own identity, but it cannot create
further specialists or touch the `draft` and `review` prompts.

Its conversation starts empty on every call. Continuity comes from its memory
file and its prompt — which is exactly why both are its own.

`dissolve_specialist` drops the roster entry. The tools and skills return to the
root's context; the folder and its memory stay on disk.

**Configuration**

| Flag | Default | Effect |
|---|---|---|
| `--provider` | `anthropic` | any provider `llm.py` supports |
| `--model` | provider default | e.g. `claude-opus-4-6` |
| `--workspace` | `agent_workspace` | memory, secrets, specialist dirs, trace log |
| `--state` | `agent_state.json` | everything it has learned |
| `--allow-network` | off | grown code may make network calls |
| `--allow-spawn` | off | enables one-off sub-agents |
| `--quiet` | off | hide the live activity lines |

Tuning constants sit at the top of `agent.py`: tool rounds per task, how often it
reviews itself, how far back it looks.

**Running for months.** Full history appends to `agent_workspace/traces.jsonl`;
only the recent tail rides in the state file, so it stays roughly fixed size.
What grows is the library of abilities, and specialists keep that out of the
prompt.

</details>

---

## Being straight with you

An assistant that rewrites itself deserves an honest README.

- **A new ability is only as good as its first draft.** The agent checks that the
  code is valid, not that it's correct. Wrong code gets caught when you correct it
  or when it reviews itself — one task later, not zero.
- **It will occasionally say it saved something it didn't.** Type `history` when
  something feels off. That log is how you catch it.
- **It can rewrite its own instructions badly.** Self-review is the safeguard, not
  a guarantee.
- **This is one agent on one machine.** No accounts, no sync, no multi-user. That
  simplicity is the point.

---

<p align="center"><sub>MIT. Take it apart.</sub></p>

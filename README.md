<p align="center">
  <img src="assets/header.svg" alt="simple-agent" width="100%">
</p>

# simple-agent

A proof of concept for two ideas:

1. An agent can be represented as one ordinary object.
2. That object can change what it is by building and rewriting its own capabilities.

The implementation deliberately avoids an agent framework. `SelfBuildingAgent` owns its
tools, skills, prompts, memory, specialists, conversation, and maintenance loop. Its current
definition is one JSON snapshot that it can edit while it works.

## Quickstart

Requires Python 3.11+.

```bash
git clone https://github.com/joshuamschultz/simple-agent
cd simple-agent
./setup.sh
```

Add one provider key to `.env`, then run:

```bash
python3 agent.py
```

The default provider is Anthropic. Select another provider with:

```bash
python3 agent.py --provider openai
```

Local providers supported by `arcllm`, such as Ollama, need no API key.

## The object

The core is `SelfBuildingAgent` in `agent.py`. It begins with a small set of levers:

- `grow_tool` writes deterministic Python and adds it to the object.
- `grow_skill` stores reusable judgment as prose.
- `update_prompt` and `update_identity` rewrite how the object thinks and acts.
- `create_specialist` moves a coherent capability subset into a persistent child view.
- `run_python` performs one-off computation without creating a permanent tool.
- `survey` reads a large workspace source in bounded parts.

The model chooses when to use those levers. The Python harness validates, executes, stores,
and exposes the result on the next model step.

```text
request
   ↓
SelfBuildingAgent.act()
   ↓
model chooses an existing capability
   ├── enough → execute it
   └── gap    → build or rewrite a capability → execute it
   ↓
answer + current state snapshot
```

## One current state

`agent_state.json` is the current agent. It contains:

```json
{
  "manifest": {},
  "skills": {},
  "prompts": {},
  "team": {},
  "since_review": 0,
  "corrections_since_review": 0,
  "traces": []
}
```

There are no component versions or embedded historical revisions. Saving writes one atomic
snapshot, so an interrupted write cannot leave a half-written agent.

`defaults.json` is only the factory seed. On first setup it is copied to
`agent_state.json`. It is never merged over an existing agent, so pulling new source does not
replace prompts or capabilities the agent has already changed.

To start a genuinely new agent, move the current state and run setup again:

```bash
mv agent_state.json my-old-agent.json
./setup.sh
```

## Workspace

Runtime data is kept separate from the definition of the object:

```text
agent_state.json            current agent definition
agent_workspace/
├── memory.data             shared user/domain memory
├── traces.jsonl            append-only task observations
├── .secrets.json           local named secrets, mode 0600
├── out_*.txt               oversized tool output
└── team/                   specialist workspaces
```

Put files the agent should use inside `agent_workspace/`. Model-directed file reads are
restricted to that directory.

## Tools and skills

A tool is one Python function stored in the manifest:

```json
{
  "percent_change": {
    "description": "Compute percentage change between two values.",
    "code": "def percent_change(self, old: float, new: float) -> float: ..."
  }
}
```

Before registration, the harness parses the source and verifies that it contains exactly one
correctly named top-level function whose first argument is `self`. A replacement uses the same
name, so callers keep one stable interface.

A skill is lighter-weight:

```json
{
  "pricing": {
    "when": "when preparing a customer price",
    "body": "Confirm quantity and deadline; always include lead time.",
    "uses": 3
  }
}
```

Only the skill name and trigger normally enter context. Its body loads on demand.

## Corrections and self-review

Use `!fb` when an answer is wrong:

```text
!fb use the existing supplier lookup before saying no supplier exists
```

The correction becomes a new task that must repair its cause—data, tool, skill, prompt,
identity, or specialist—not merely apologize.

Every few tasks, maintenance reviews recent real exchanges alongside the current capability
inventory. It looks for repeated manual work, unused or overlapping capabilities, missed
skills, recurring corrections, and unreachable stored data. Type `review` to run it now.

## Terminal commands

```text
!secret NAME       store a named secret without putting its value in chat
!secrets           list secret names
!fb TEXT           correct the last result and repair its cause
review             run maintenance now
new                clear the conversation only
tools              list grown tools
skills             list skills
skill NAME         read a skill
team               list specialists
identity           show the current assembled system prompt
raw                show shared memory
history            show recent task traces
cost               show this session's model cost
exit               save and exit
```

## Execution and privacy boundary

Generated Python runs in a separate process with a timeout, but it is not a hostile-code
sandbox. It runs with the local user's operating-system authority. Use this project as trusted
personal automation; use a disposable container before pointing an untrusted model at valuable
files or credentials.

Network use is off by instruction by default and enabled with `--allow-network`; this flag is
not an operating-system firewall. Filesystem access initiated through `survey` stays inside the
workspace, but arbitrary generated Python cannot be securely confined by Python itself.

Anything placed into model context is sent to the configured model provider. That may include
user messages, selected workspace file contents, tool results, memory, prompts, and review
inventory. Known stored secret values are redacted from tool output and traces, but do not put
secrets directly into chat. Use `!secret NAME` instead.

## Development

Install the pinned development tools:

```bash
uv pip install --python .venv/bin/python '.[dev]'
```

Run the gates:

```bash
.venv/bin/ruff check agent.py llm.py tests
.venv/bin/mypy agent.py llm.py
.venv/bin/python -m pytest --cov=agent --cov=llm
```

The tests use a fake client and do not call a model provider.

## Project layout

```text
agent.py          object, self-modification loop, and terminal UI
llm.py            provider boundary
defaults.json     first-run seed only
setup.sh          environment and first-run state creation
tests/            focused harness tests
vendor/           pinned local arcllm distribution
```

The project remains intentionally compact. Split a subsystem only when doing so makes the two
core ideas easier to see or independently test—not simply to make files shorter.

## Current limitations

- Generated tools are syntax- and shape-checked, not proven correct.
- Self-review is model judgment, not a correctness guarantee.
- The state is single-user and local; there is no synchronization or concurrent writer support.
- Conversation compaction is lossy. Durable facts belong in memory, not only in chat.
- Secrets are access-restricted plaintext, not OS-keychain entries.

MIT licensed. Take it apart.

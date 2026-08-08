"""
Self-building agent, v5 — ONE FILE, ONE CLASS.

The harness owns: growth (draft->validate->register), sandboxed execution,
secrets custody, raw storage, the team registry, feedback, and a
maintenance cadence. Everything with judgment in it — what to build, when
to promote a tool family into a standing specialist, when to dissolve one,
what to remember, what its own prompt should say — belongs to the model.

Two kinds of growth:
- TOOLS are Python, for work that must be deterministic. Drafted, validated,
  registered, then run in a subprocess.
- SKILLS are prose, for the judgment code can't hold: procedures, house
  rules, checklists, gotchas. Only each skill's one-line index entry rides
  in the prompt; the body loads on demand, so the library can grow large
  without the prompt growing with it.

Team model (mixture-of-experts):
- ONE central registry — every grown tool and skill, wherever it was grown,
  lives here. Single source of truth, no cross-specialist drift.
- Specialists are persistent named VIEWS over that registry: an identity, a
  tool subset, a skill subset, and their own memory file that persists
  between calls (this is what tunes over months). Between calls a specialist
  is just a registry entry — agents are objects, nothing runs idle.
- Whatever a specialist owns leaves the root's context — the root SHRINKS as
  the team grows. That's the overload relief.
- Ad-hoc spawn_agent stays for one-off subtasks: assembled, run, wound
  down, nothing registered.

LLM calls go through arcllm — one provider-agnostic client, direct HTTP, no
vendor SDK. Swap `--provider openai` (or google, groq, ollama, ...) and
nothing else in this file changes. Retry, telemetry (per-call USD cost),
PII redaction and audit are arcllm modules, toggled at load_model().

Run:  export ANTHROPIC_API_KEY=...  &&  python3 agent.py
Sandbox note: subprocess isolation stops accidents (hangs, crashes, casual
vault access), not malice — same user, same filesystem. Container-wrap the
identical runner before real stakes.
"""

from __future__ import annotations
import asyncio
import importlib.util
import inspect
import json
import os
import stat
import subprocess
import sys
import types
import uuid

_HERE = os.path.dirname(os.path.abspath(__file__))

# `python3 agent.py` from any shell should just work. If the interpreter that
# started us can't see arcllm but this project's venv can, hand off to it.
if importlib.util.find_spec("arcllm") is None:
    _venv_dir = os.path.join(_HERE, ".venv")
    _venv = os.path.join(_venv_dir, "bin", "python")
    # Only when THIS file is the entrypoint — re-execing on an `import agent`
    # would replace the caller's program with our REPL.
    _entry = os.path.realpath(sys.argv[0] or "") == os.path.realpath(__file__)
    # Compare prefixes, not interpreter paths: a uv venv's `python` is a
    # symlink to the base interpreter, so realpath() would call them equal.
    if _entry and os.path.exists(_venv) and os.path.realpath(sys.prefix) != os.path.realpath(_venv_dir):
        os.execv(_venv, [_venv, os.path.abspath(__file__), *sys.argv[1:]])
    sys.exit("arcllm not found. Run ./setup.sh once, then `python3 agent.py`.")

from dotenv import load_dotenv  # noqa: E402

# This project's .env wins over whatever the shell exports — a stale or
# mislabelled key in the environment otherwise silently 401s and (with a
# fallback chain configured) gets answered by a different provider entirely.
load_dotenv(os.path.join(_HERE, ".env"), override=True)

from arcllm import (LLMResponse, Message, TextBlock, Tool, ToolResultBlock,  # noqa: E402
                    ToolUseBlock, load_model)

MAX_TOKENS = 50000         # a cap, not a charge — only real output tokens bill
MAX_TOOL_ITERS = 24
CONTEXT_MESSAGES = 60      # running conversation kept in front of the model
MAX_SPAWN_DEPTH = 2
REVIEW_EVERY = 5                # review often; patterns hide in the gaps
REVIEW_WINDOW = 50              # but always look back further than you review
CORRECTIONS_BEFORE_REVIEW = 2   # or sooner, when the user has had to correct you
SANDBOX_TIMEOUT = 30
TRACES_IN_STATE = 200      # older traces live in the append-only log, not state

# Every harness-owned failure string starts with this. A tool result either
# begins with FAILED: or it worked — that is the whole outcome contract, and
# it is why traces can record success without parsing anything.
FAILED = "FAILED: "

_JSON_TYPE = {str: "string", int: "integer", float: "number", bool: "boolean",
              list: "array", dict: "object"}

_LOOP: asyncio.AbstractEventLoop | None = None


def _sync(coro):
    """Run one arcllm coroutine from this synchronous agent.

    A single long-lived loop, not asyncio.run() per call — the adapter's
    httpx connection pool is bound to the loop that created it.
    """
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
    return _LOOP.run_until_complete(coro)

SEED_IDENTITY = """You are an agent that builds its own capabilities, memory, team, and even this system prompt as tasks require.

Storage: grown code can call self._raw_read() / self._raw_write(content) on your one raw storage file — you decide the format, and you are expected to keep changing it. The shape you pick on day one is the shape you understood least; when what you're storing outgrows it, migrate every record into a better one rather than bolting the new thing onto the old. Read a storage tool with read_tool before you change how anything is stored, and replace it under the same name. Secrets: grown code reads credentials via self._secret("NAME"); never ask users to paste secret values into chat — they use the local !secret command.

You grow in TWO ways, and picking the right one matters:

TOOLS (grow_tool) are Python. Use one whenever the work is deterministic — the same input must always give the same output. Parsing, math, storage, formatting, calling an API. Never do in prose what code can do exactly. Grown code has full Python: any import, open() in your workspace, network if enabled. It's callable on your next step.

SKILLS (grow_skill) are durable instructions to yourself, in prose, for the parts code can't decide: a procedure worth repeating, house style, domain rules, a checklist, a gotcha that cost you a mistake once. Write a skill the moment you notice yourself re-deriving the same judgment, and rewrite it whenever you learn something that would have made it better. Only each skill's name and when_to_use sit in your prompt; read_skill loads the full text, so read one BEFORE doing the work it covers, not after. Because bodies load on demand, a large skill library costs you almost nothing to carry.

The pairing is the point: a skill says how to think about vendor risk, a tool computes the score. Neither substitutes for the other.

Team: when a family of tools and skills around one domain (vendors, customers, inventory...) reaches critical mass, promote it: create_specialist registers a standing expert with its own prompt, tool subset, skill subset, and persistent memory — all of it leaves your context, keeping you fast. Route matching tasks to it with call_specialist. Dissolve stale specialists; their tools and skills return to you. For one-off subtasks use spawn_agent — assembled, run, gone, nothing registered. Your skill index and team roster are appended below this prompt each turn.

This prompt is yours: update_identity replaces it entirely. It's sent every turn — keep it short and current; record what you've built and where things live.

Every tool result either worked or begins with "FAILED: ". A FAILED result is information, not noise — read it, fix the cause, and don't repeat the same call. Your recent history, including which calls failed and every correction the user has given you, is summarized for you during maintenance. A correction is ground truth — reconcile whatever produced the error."""

_RUNNER = '''
import json, os, sys
class AgentShim:
    def _raw_read(self):
        try:
            with open("memory.data") as f: return f.read()
        except FileNotFoundError: return ""
    def _raw_write(self, content):
        with open("memory.data", "w") as f: f.write(content)
    def _secret(self, name):
        return os.environ.get("SECRET_" + name, "")
import types as _t
_ns = {{}}
exec({manifest_code!r}, globals(), _ns)
_shim = AgentShim()
for _n, _f in _ns.items():
    if callable(_f): setattr(_shim, _n, _t.MethodType(_f, _shim))
_r = getattr(_shim, {fn_name!r})(**json.loads(sys.argv[1]))
print("__RESULT__" + json.dumps({{"result": str(_r)}}))
'''


class SelfBuildingAgent:
    _META_ROOT = ("grow_tool", "read_tool", "grow_skill", "read_skill", "forget_skill",
                   "update_identity", "create_specialist", "call_specialist",
                   "dissolve_specialist", "spawn_agent")
    _META_CHILD = ("grow_tool", "read_tool", "grow_skill", "read_skill",
                    "update_identity", "spawn_agent")

    def __init__(self, client=None, workspace: str = "agent_workspace",
                 allow_network: bool = False, allow_spawn: bool = False,
                 provider: str = "anthropic", model: str | None = None):
        self.client = client          # an arcllm LLMProvider; built lazily if None
        self.provider = provider
        self.model = model            # None -> provider's default_model
        self.cost_usd = 0.0
        self.workspace = workspace
        os.makedirs(workspace, exist_ok=True)
        self.identity = SEED_IDENTITY
        self.manifest: dict[str, str] = {}          # ALL grown tools, central
        self.skills: dict[str, dict] = {}           # name -> {when, body, uses}
        self.team: dict[str, dict] = {}             # name -> {identity, tools, skills, description}
        self.traces: list[dict] = []
        self.messages: list[Message] = []      # the live conversation
        self.allow_network = allow_network
        self.allow_spawn = allow_spawn
        self.tasks_since_maintenance = 0
        self.corrections_since_review = 0
        self._spawn_depth = 0
        self._vault_path = os.path.join(workspace, ".secrets.json")
        if not os.path.exists(self._vault_path):
            with open(self._vault_path, "w") as f:
                f.write("{}")
            os.chmod(self._vault_path, stat.S_IRUSR | stat.S_IWUSR)

    # ---------------- secrets vault ----------------

    def _secrets(self) -> dict:
        with open(self._vault_path) as f:
            return json.load(f)

    def secret_store(self, name: str, value: str):
        """Harness/REPL-side only — values must arrive via a local channel
        (getpass), never through chat or model-visible text."""
        d = self._secrets()
        d[name] = value
        with open(self._vault_path, "w") as f:
            json.dump(d, f)
        os.chmod(self._vault_path, stat.S_IRUSR | stat.S_IWUSR)

    def secret_names(self) -> list[str]:
        return list(self._secrets().keys())

    def _redact(self, text: str) -> str:
        for name, value in self._secrets().items():
            if value and value in text:
                text = text.replace(value, f"[REDACTED:{name}]")
        return text

    # ---------------- raw storage ----------------

    def _raw_read(self) -> str:
        try:
            with open(os.path.join(self.workspace, "memory.data")) as f:
                return f.read()
        except FileNotFoundError:
            return ""

    def _raw_write(self, content: str) -> None:
        with open(os.path.join(self.workspace, "memory.data"), "w") as f:
            f.write(content)

    # ---------------- sandboxed execution ----------------

    def _validate(self, code: str, fn_name: str) -> bool:
        try:
            compile(code, "<grown>", "exec")
        except SyntaxError:
            return False
        return f"def {fn_name}(" in code

    def _run_sandboxed(self, manifest: dict, fn_name: str, kwargs: dict) -> str:
        runner = _RUNNER.format(manifest_code="\n\n".join(manifest.values()), fn_name=fn_name)
        env = {"PATH": os.environ.get("PATH", ""),
               **{f"SECRET_{k}": v for k, v in self._secrets().items()}}
        try:
            proc = subprocess.run([sys.executable, "-c", runner, json.dumps(kwargs)],
                                    capture_output=True, text=True, timeout=SANDBOX_TIMEOUT,
                                    cwd=self.workspace, env=env)
        except subprocess.TimeoutExpired:
            return f"{FAILED}exceeded {SANDBOX_TIMEOUT}s timeout"
        out = proc.stdout
        if "__RESULT__" in out:
            prefix, payload = out.split("__RESULT__", 1)
            try:
                result = json.loads(payload.strip())["result"]
            except (json.JSONDecodeError, KeyError):
                result = payload.strip()
            combined = (prefix.strip() + "\n" + result).strip() if prefix.strip() else result
            return self._redact(combined)
        return self._redact(f"{FAILED}{(proc.stderr or 'no output').strip()[-800:]}")

    # ---------------- LLM calls via arcllm ----------------

    def _client(self):
        """The arcllm model object. Long-lived — one connection pool, shared
        with every specialist and sub-agent this agent creates."""
        if self.client is None:
            opts = {"telemetry": True,   # per-call USD cost
                    "retry": True}       # backoff on 429/5xx
            # agent_label labels traces per agent; older arcllm releases don't
            # take it, and it isn't worth pinning a version over.
            if "agent_label" in inspect.signature(load_model).parameters:
                opts["agent_label"] = f"selfbuilder:{self.workspace}"
            self.client = load_model(self.provider, self.model, **opts)
        return self.client

    def _invoke(self, messages: list[Message], tools: list[Tool] | None = None) -> LLMResponse:
        resp = _sync(self._client().invoke(messages, tools or None, max_tokens=MAX_TOKENS))
        self.cost_usd += resp.cost_usd or 0.0
        return resp

    def _complete(self, system: str, messages: list[Message], tools: list[Tool]) -> LLMResponse:
        return self._invoke([Message(role="system", content=system), *messages], tools)

    def _tool_contract(self, name: str, code: str) -> str:
        """One line describing a tool the way a caller needs it: how to call
        it and what it promises. Never its body."""
        sig = code.split("\n", 1)[0].strip()
        sig = sig[4:-1] if sig.startswith("def ") and sig.endswith(":") else name
        doc = ""
        m = __import__("re").search(r'"""(.*?)"""', code, __import__("re").S)
        if m:
            doc = " ".join(m.group(1).split())
        return f"- self.{sig}\n    {doc or '(no description)'}"

    def _draft_method(self, gap_description: str) -> dict:
        import re as _re
        # The interface, not the implementation. A drafter that sees source
        # code and stored bytes copies whatever shape was invented first —
        # which is the shape from when the least was known. Names, signatures
        # and contracts are what a caller actually needs, and they leave the
        # author free to build something better than what is already there.
        existing = "\n".join(self._tool_contract(n, c) for n, c in self.manifest.items()) or "(none yet)"
        network = ("You may use urllib/sockets and any installed package to call APIs — network is enabled."
                   if self.allow_network else "No network calls.")
        prompt = f"""A capability gap: {gap_description}

Tools that already exist. Call any of them from your code as self.<name>(...):
{existing}

If one of those already owns this job, the right move is usually to REPLACE it:
draft under its exact name and yours supersedes it. Do that rather than adding a
near-duplicate beside it.

Storage: self._raw_read() and self._raw_write(content) share ONE store across
every tool. So read it, change only the part your tool owns, and write the whole
thing back — never blank another tool's data. Give what you own its own
namespace, and put a type on records rather than leaving different kinds of
thing jumbled together at the top level. If you are replacing a storage tool and
the old shape was wrong, migrate it: read every existing record, move it into
the better shape, write it back. Nothing already stored may be lost. The store
is expected to keep improving; it should never be frozen at whatever the first
tool happened to invent.

Also available: self._secret(name) for credentials. Full Python — any import,
open() on files. {network}

Never assume a working directory. If your tool touches a path, take that path as
a parameter so the caller supplies it.

Make every action work on its own: a parameter only some actions need must have
a default, never be required.

Respond with ONLY a JSON object, no prose, no fences:
{{"name": "<snake_case_name>", "code": "def <name>(self, <typed params>) -> <type>:\\n    \\"\\"\\"<one line: what it does, when to call it, and what it owns in the store>\\"\\"\\"\\n    <body>"}}
The docstring is the ONLY thing other tools and future drafts will see about
this tool, so make it a contract, not a label.
First parameter must be self. Type-hint every parameter (str/int/float/bool/list/dict)."""
        resp = self._invoke([Message(role="user", content=prompt)])
        cleaned = _re.sub(r"^```(?:json)?|```$", "", (resp.content or "").strip(),
                          flags=_re.MULTILINE).strip()
        return json.loads(cleaned)

    # ---------------- meta-tools (the model's levers) ----------------

    def grow_tool(self, description: str) -> str:
        """Draft a new Python method from a concrete description (inputs,
        output, behavior) and make it callable on your next step."""
        try:
            spec = self._draft_method(description)
            if not self._validate(spec["code"], spec["name"]):
                return f"{FAILED}draft did not validate — try a more concrete description."
            self.manifest[spec["name"]] = spec["code"]
            return f"Grew {spec['name']}. Callable from your next step."
        except Exception as e:
            return f"{FAILED}draft error: {e}"

    def read_tool(self, name: str) -> str:
        """Read a grown tool's source. Do this before changing how something
        is stored or retrieved — grow_tool with the same name REPLACES it, so
        repairing the tool you have beats growing a second one beside it."""
        code = self.manifest.get(name)
        if not code:
            return f"{FAILED}no tool named '{name}'. You have: {list(self.manifest)}"
        return code

    def grow_skill(self, name: str, when_to_use: str, body: str) -> str:
        """Write or rewrite a SKILL: durable instructions for yourself, in
        prose, for work that judgment does rather than code. Procedures, house
        style, domain rules, checklists, hard-won gotchas. Only the name and
        when_to_use sit in your prompt; the body loads on demand via
        read_skill, so a large library stays cheap. Rewrite a skill whenever
        you learn something that would have made it better."""
        existing = self.skills.get(name, {})
        self.skills[name] = {"when": when_to_use, "body": body, "uses": existing.get("uses", 0)}
        verb = "Rewrote" if existing else "Wrote"
        return f"{verb} skill '{name}' ({len(body)} chars). It is in your index; read_skill loads it."

    def read_skill(self, name: str) -> str:
        """Load one skill's full text into this turn. Read a skill BEFORE
        doing the work it covers, not after."""
        skill = self.skills.get(name)
        if not skill:
            return f"{FAILED}no skill named '{name}'. Index: {list(self.skills.keys())}"
        skill["uses"] = skill.get("uses", 0) + 1
        return f"SKILL {name} — {skill['when']}\n\n{skill['body']}"

    def forget_skill(self, name: str) -> str:
        """Delete a skill that is wrong, stale, or superseded. Prefer
        rewriting with grow_skill; delete only when it should not exist."""
        if name not in self.skills:
            return f"{FAILED}no skill named '{name}'."
        del self.skills[name]
        return f"Forgot skill '{name}'."

    def update_identity(self, new_identity: str) -> str:
        """Replace your entire system prompt. Full replacement, not append —
        carry forward what still matters. Takes effect next turn."""
        self.identity = new_identity
        return f"Identity updated ({len(new_identity)} chars)."

    def create_specialist(self, name: str, identity: str, tools: list, description: str,
                          skills: list = None) -> str:
        """Promote a tool-and-skill family into a standing specialist: its own
        system prompt, the named tool subset and skill subset (both moved out
        of your context), a one-line description for your roster, and its own
        persistent memory that accumulates across every call to it."""
        skills = list(skills or [])
        missing = [t for t in tools if t not in self.manifest]
        if missing:
            return f"{FAILED}unknown tools {missing} — grow them before promoting them."
        unknown = [s for s in skills if s not in self.skills]
        if unknown:
            return f"{FAILED}unknown skills {unknown} — write them with grow_skill first."
        self.team[name] = {"identity": identity, "tools": list(tools), "skills": skills,
                           "description": description}
        os.makedirs(os.path.join(self.workspace, "team", name), exist_ok=True)
        return (f"Specialist '{name}' registered with {len(tools)} tools and {len(skills)} skills. "
                f"Route matching tasks to it with call_specialist.")

    def call_specialist(self, name: str, task: str) -> str:
        """Run one task on a registered specialist. It gets its identity, its
        tool subset, and its own persistent memory. Anything it grows joins
        the central registry under its ownership."""
        spec = self.team.get(name)
        if not spec:
            return f"{FAILED}no specialist named '{name}'. Roster: {list(self.team.keys())}"
        child = SelfBuildingAgent(client=self._client(), workspace=os.path.join(self.workspace, "team", name),
                                    allow_network=self.allow_network, allow_spawn=self.allow_spawn,
                                    provider=self.provider, model=self.model)
        child._vault_path = self._vault_path  # one vault, harness-owned
        child._spawn_depth = self._spawn_depth + 1
        child.manifest = {t: self.manifest[t] for t in spec["tools"] if t in self.manifest}
        child.skills = {s: self.skills[s] for s in spec.get("skills", []) if s in self.skills}
        child.identity = spec["identity"]
        result = child.run(task)
        self.cost_usd += child.cost_usd
        for tool_name, code in child.manifest.items():   # merge growth back to the central registry
            if tool_name not in self.manifest:
                self.manifest[tool_name] = code
                spec["tools"].append(tool_name)
        for skill_name, skill in child.skills.items():   # skills the specialist wrote or refined
            if skill_name not in self.skills:
                spec.setdefault("skills", []).append(skill_name)
            self.skills[skill_name] = skill
        if child.identity != spec["identity"]:            # specialists tune their own prompts too
            spec["identity"] = child.identity
        return result["output"]

    def dissolve_specialist(self, name: str) -> str:
        """Remove a stale specialist from the roster. Its tools stay in the
        central registry and return to your own schema; its memory file is
        kept on disk in case it's re-created later."""
        if name not in self.team:
            return f"{FAILED}no specialist named '{name}'."
        del self.team[name]
        return (f"Dissolved '{name}'. Its tools and skills are back in your context; "
                f"its memory remains on disk.")

    def spawn_agent(self, identity: str, task: str, tools: list = None, seed_memory: str = "") -> str:
        """One-off sub-agent: injected prompt, optional tool subset (all
        yours if unspecified), optional seed memory. Runs one task, is wound
        down. Nothing registered; its growth is reported, not kept."""
        if self._spawn_depth >= MAX_SPAWN_DEPTH:
            return f"{FAILED}max recursion depth reached."
        child = SelfBuildingAgent(client=self._client(),
                                    workspace=os.path.join(self.workspace, f"adhoc_{uuid.uuid4().hex[:6]}"),
                                    allow_network=self.allow_network, allow_spawn=self.allow_spawn,
                                    provider=self.provider, model=self.model)
        child._vault_path = self._vault_path
        child._spawn_depth = self._spawn_depth + 1
        names = tools if tools else list(self.manifest.keys())
        child.manifest = {n: self.manifest[n] for n in names if n in self.manifest}
        child.skills = dict(self.skills)      # a one-off still gets the house knowledge
        child.identity = identity
        if seed_memory:
            child._raw_write(seed_memory)
        result = child.run(task)
        self.cost_usd += child.cost_usd
        new = [n for n in child.manifest if n not in names]
        return result["output"] + (f" [sub-agent also grew (not kept): {', '.join(new)}]" if new else "")

    # ---------------- schemas & system prompt ----------------

    def _meta_names(self):
        names = self._META_ROOT if self._spawn_depth == 0 else self._META_CHILD
        return [n for n in names if n != "spawn_agent" or
                (self.allow_spawn and self._spawn_depth < MAX_SPAWN_DEPTH)]

    def _schema_for(self, name: str, fn, skip_self=False) -> Tool:
        sig = inspect.signature(fn)
        props, required = {}, []
        for pname, param in sig.parameters.items():
            if skip_self and pname == "self":
                continue
            ann = param.annotation if param.annotation is not inspect.Parameter.empty else str
            props[pname] = {"type": _JSON_TYPE.get(ann, "string")}
            if param.default is inspect.Parameter.empty:
                required.append(pname)
        doc = (inspect.getdoc(fn) or "").strip() or f"Call {name}."
        return Tool(name=name, description=doc,
                    parameters={"type": "object", "properties": props, "required": required})

    def _assigned_tools(self) -> set:
        out = set()
        for spec in self.team.values():
            out.update(spec["tools"])
        return out

    def _tools_schema(self) -> list[Tool]:
        schema = [self._schema_for(name, getattr(self, name)) for name in self._meta_names()]
        assigned = self._assigned_tools() if self._spawn_depth == 0 else set()
        for name, code in self.manifest.items():
            if name in assigned:
                continue  # a specialist owns it — out of the root's context
            try:
                ns: dict = {}
                exec(compile(code, "<schema>", "exec"), {"__builtins__": {}}, ns)
                fn = ns.get(name)
                if not fn:
                    continue
                schema.append(self._schema_for(name, fn, skip_self=True))
            except Exception:
                continue
        return schema

    def _assigned_skills(self) -> set:
        out = set()
        for spec in self.team.values():
            out.update(spec.get("skills", []))
        return out

    def _build_system(self) -> str:
        parts = [self.identity]
        assigned = self._assigned_skills() if self._spawn_depth == 0 else set()
        index = [f"- {n}: {s['when']}" for n, s in self.skills.items() if n not in assigned]
        if index:
            # Only the index rides in the prompt. Bodies load through
            # read_skill, so the library can grow without the prompt growing.
            parts.append("Your skills (read_skill loads the full text — read one BEFORE "
                          "doing the work it covers):\n" + "\n".join(index))
        if self.team:
            roster = "\n".join(
                f"- {n}: {s['description']} ({len(s['tools'])} tools, "
                f"{len(s.get('skills', []))} skills)" for n, s in self.team.items())
            parts.append(f"Your team (route matching tasks with call_specialist):\n{roster}")
        return "\n\n".join(parts)

    # ---------------- the loop ----------------

    def act(self, task: str, fresh: bool = False):
        """One task, inside the running conversation.

        `fresh` isolates meta-work — a review, a draft — so it reasons about
        the session without becoming part of it.
        """
        if fresh:
            messages: list[Message] = [Message(role="user", content=task)]
        else:
            self.messages.append(Message(role="user", content=task))
            messages = self.messages
        calls: list[dict] = []
        for _ in range(MAX_TOOL_ITERS):
            resp = self._complete(self._build_system(), messages, self._tools_schema())
            if resp.stop_reason != "tool_use":
                # A turn cut off at the token cap used to come back as an empty
                # string and get filed as a success. Say so instead, and let
                # run() record it as the failure it is.
                if resp.stop_reason == "max_tokens" and not resp.content:
                    answer = f"[no final answer — hit the {MAX_TOKENS} token cap mid-response]"
                else:
                    answer = resp.content or ""
                if not fresh:
                    messages.append(Message(role="assistant", content=answer or "(no answer)"))
                    self._trim_conversation()
                return answer, calls
            blocks = ([TextBlock(text=resp.content)] if resp.content else []) + [
                ToolUseBlock(id=c.id, name=c.name, arguments=c.arguments) for c in resp.tool_calls]
            messages.append(Message(role="assistant", content=blocks))
            tool_results = []
            meta = set(self._meta_names())
            for call in resp.tool_calls:
                try:
                    if call.name in meta:
                        result = getattr(self, call.name)(**call.arguments)
                    elif call.name in self.manifest:
                        result = self._run_sandboxed(self.manifest, call.name, call.arguments)
                    else:
                        result = f"{FAILED}no such tool: {call.name}"
                except Exception as e:
                    result = f"{FAILED}{type(e).__name__}: {e}"
                result = str(result)
                calls.append({"tool": call.name, "ok": not result.startswith(FAILED),
                              "args": call.arguments, "result": result[:200]})
                tool_results.append(ToolResultBlock(tool_use_id=call.id, content=result))
            messages.append(Message(role="tool", content=tool_results))
        if not fresh:
            self._trim_conversation()
        return "[no final answer — exceeded tool-use iteration budget]", calls

    def _trim_conversation(self) -> None:
        """Bound the running conversation, cutting only at user turns.

        Anthropic requires every tool_use block to be answered by a
        tool_result, so a trim that lands mid-exchange produces an API error.
        Dropping whole exchanges from the front is the only safe cut.
        """
        while len(self.messages) > CONTEXT_MESSAGES:
            del self.messages[0]
            while self.messages and self.messages[0].role != "user":
                del self.messages[0]

    # ---------------- traces ----------------

    def _record(self, task: str, calls: list[dict], output: str, success: bool) -> dict:
        """Append one trace. The full history is an append-only log on disk;
        only the recent tail rides along in the state file."""
        trace = {"task": task, "output": output[:600], "used": [c["tool"] for c in calls],
                 "outcome": success, "correction": None, "calls": calls}
        self.traces.append(trace)
        try:
            with open(os.path.join(self.workspace, "traces.jsonl"), "a") as f:
                f.write(json.dumps(trace) + "\n")
        except OSError:
            pass          # the log is for hindsight; never let it break a task
        return trace

    def _session_review(self, limit: int = REVIEW_WINDOW) -> str:
        """The recent session, verbatim, for review.

        Counts and error rates describe whether the machinery ran. They say
        nothing about whether the user got what they asked for — and that is
        the only question worth reviewing. So this hands back the actual
        exchange: what was asked, what was answered, what the tools returned.
        A question asked twice, an answer the tool results don't support, two
        tools returning the same list: all of it is visible in the transcript
        and none of it is visible in a tally.
        """
        recent = [t for t in self.traces[-limit:] if t["task"] != "[maintenance]"]
        if not recent:
            return "(no session yet)"
        out = []
        for i, t in enumerate(recent, 1):
            out.append(f"--- exchange {i} ---")
            out.append(f"USER: {t['task'][:300]}")
            for c in t.get("calls", []):
                mark = "  [FAILED]" if not c["ok"] else ""
                out.append(f"  tool {c['tool']}({json.dumps(c['args'])[:80]})"
                            f" -> {c['result'][:130]}{mark}")
            out.append(f"YOU: {(t.get('output') or '')[:250]}")
            if t.get("correction"):
                out.append(f"  ** THE USER THEN CORRECTED YOU: {t['correction']}")
        usage: dict[str, int] = {}
        for t in recent:
            for c in t.get("calls", []):
                usage[c["tool"]] = usage.get(c["tool"], 0) + 1
        unused = [n for n in self.manifest if n not in usage]
        if unused:
            out.append(f"\nTools you never reached for: {', '.join(unused)}")
        if self.skills:
            out.append("Skills and how often you read them: " + ", ".join(
                f"{n} {s.get('uses', 0)}x" for n, s in self.skills.items()))
        return "\n".join(out)

    def _trace_digest(self, limit: int = 25) -> str:
        """Recent history, compressed, for the maintenance pass to read.

        The maintenance prompt has always asked the agent to review its
        traces; without this the traces were never actually in the prompt.
        Failures are listed verbatim because they are the whole point.
        """
        recent = [t for t in self.traces[-limit:] if t["task"] != "[maintenance]"]
        if not recent:
            return "(no history yet)"
        usage: dict[str, list[int]] = {}
        failures, corrections = [], []
        for t in recent:
            for c in t.get("calls", []):
                tally = usage.setdefault(c["tool"], [0, 0])
                tally[0] += 1
                if not c["ok"]:
                    tally[1] += 1
                    failures.append(f'  {c["tool"]}({json.dumps(c["args"])[:80]}) -> {c["result"][:110]}')
            if t.get("correction"):
                corrections.append(f'  task {t["task"][:60]!r} -> user said: {t["correction"][:110]}')
        lines = [f"Last {len(recent)} tasks."]
        if usage:
            lines.append("Tool use (calls/failed): " + ", ".join(
                f"{n} {c[0]}/{c[1]}" for n, c in sorted(usage.items(), key=lambda kv: -kv[1][0])))
        unused = [n for n in self.manifest if n not in usage]
        if unused:
            lines.append(f"Tools never called in this window: {', '.join(unused)}")
        if self.skills:
            lines.append("Skills (lifetime reads): " + ", ".join(
                f"{n} {s.get('uses', 0)}" for n, s in self.skills.items()))
        repeats: dict[str, int] = {}
        for t in recent:
            for c in t.get("calls", []):
                if c["tool"] == "grow_tool":
                    repeats[str(c["args"])[:60]] = repeats.get(str(c["args"])[:60], 0) + 1
        if any(v > 1 for v in repeats.values()):
            lines.append("You grew near-identical tools more than once — that is a skill "
                          "waiting to be written, or a tool waiting to be generalized.")
        if failures:
            lines.append("FAILED CALLS — fix the cause, do not repeat them:")
            lines.extend(failures[-12:])
        if corrections:
            lines.append("User corrections (ground truth):")
            lines.extend(corrections[-5:])
        return "\n".join(lines)

    def _act_review(self):
        """The periodic review.

        Deliberately short. A long instruction list produced an empty answer;
        the same transcript with one blunt question produced the actual root
        cause in a sentence. The transcript carries the evidence, so the
        prompt only has to ask the right thing of it.
        """
        return self.act(
            f"REVIEW — your last {REVIEW_WINDOW} exchanges, verbatim. What the user asked, what "
            f"your tools returned, what you answered.\n\n{self._session_review()}\n\n"
            "Read the whole run, not one exchange at a time. The problems worth fixing only show "
            "up across exchanges: a question asked twice, a rephrase, a correction, an answer the "
            "tool results never supported, two tools returning the same data, a lookup that misses "
            "a name that is plainly there, judgment you worked out once and then worked out again.\n\n"
            "One question: WHERE DID THE USER NOT GET WHAT THEY WANTED? A call can succeed and "
            "still be wrong.\n\n"
            "Then fix the cause so it cannot recur — read_tool and replace a broken tool under its "
            "own name, migrate the store if its shape no longer fits, grow_skill so a judgment "
            "survives, repair data a bad tool corrupted, promote or dissolve a specialist, "
            "update_identity to match what actually exists.\n\n"
            "Finish with 2-3 plain sentences to the user: what you got wrong, what is different now.",
            fresh=True)

    def review(self) -> dict:
        """Run the review immediately instead of waiting for the cadence."""
        self.tasks_since_maintenance = 0
        self.corrections_since_review = 0
        output, calls = self._act_review()
        self._record("[maintenance]", calls, output, True)
        return {"output": output, "calls": calls}

    def run(self, task: str, grader=None) -> dict:
        output, calls = self.act(task)
        success = grader(output) if grader else not output.startswith("[no final answer")
        self._record(task, calls, output, success)
        if self._spawn_depth == 0:
            self.tasks_since_maintenance += 1
            # Cadence OR signal. Two corrections in a row means something is
            # wrong now, and waiting another twenty tasks to look at it is how
            # a small misunderstanding becomes a habit.
            if (self.tasks_since_maintenance >= REVIEW_EVERY
                    or self.corrections_since_review >= CORRECTIONS_BEFORE_REVIEW):
                self.tasks_since_maintenance = 0
                self.corrections_since_review = 0
                mout, mcalls = self._act_review()
                self._record("[maintenance]", mcalls, mout, True)
        return {"output": output, "success": success, "used": [c["tool"] for c in calls],
                "calls": calls}

    def feedback(self, correction: str) -> dict:
        """User correction — ground truth. Marks the last real trace failed
        and runs one reconciliation turn so the fix persists."""
        last = next((t for t in reversed(self.traces) if t["task"] != "[maintenance]"), None)
        prior = f"Task: {last['task']}" if last else "(no prior task)"
        if last:
            last["outcome"] = False
            last["correction"] = correction
        self.corrections_since_review += 1
        output, calls = self.act(
            f"CORRECTION from the user. {prior}\nThe user says: {correction}\n"
            f"This is ground truth. Fix whatever produced the error — stored memory, a tool, a specialist, "
            f"or your identity — so it stays fixed. Confirm what you changed.\n\n"
            f"Your recent history, for context:\n{self._trace_digest(10)}")
        self._record(f"[correction] {correction}", calls, output, True)
        return {"output": output, "calls": calls}

    # ---------------- persistence ----------------

    def save(self, path: str):
        # Only the recent tail of history lives here. The rest is already in
        # workspace/traces.jsonl, so state.json stays a roughly fixed size no
        # matter how many months this agent has been running.
        with open(path, "w") as f:
            json.dump({"manifest": self.manifest, "skills": self.skills,
                        "identity": self.identity, "team": self.team,
                        "since_review": self.tasks_since_maintenance,
                        "corrections_since_review": self.corrections_since_review,
                        "traces": self.traces[-TRACES_IN_STATE:]}, f, indent=2)

    def load(self, path: str):
        with open(path) as f:
            data = json.load(f)
        self.manifest = {n: c for n, c in data.get("manifest", {}).items() if self._validate(c, n)}
        self.skills = data.get("skills", {})
        self.identity = data.get("identity", self.identity)
        self.team = data.get("team", {})
        self.traces = data.get("traces", [])[-TRACES_IN_STATE:]
        # Without this the counter reset on every restart and the review
        # simply never fired.
        # The conversation is deliberately NOT restored. A restart is a clean
        # slate for the thread; what persists is what was learned — tools,
        # skills, memory, identity.
        self.messages = []
        self.tasks_since_maintenance = data.get("since_review", 0)
        self.corrections_since_review = data.get("corrections_since_review", 0)


# ---------------- REPL ----------------

def _main():
    import argparse, getpass
    p = argparse.ArgumentParser()
    p.add_argument("--allow-network", action="store_true")
    p.add_argument("--allow-spawn", action="store_true")
    p.add_argument("--workspace", default="agent_workspace")
    p.add_argument("--state", default="agent_state.json")
    p.add_argument("--provider", default="anthropic", help="any arcllm provider")
    p.add_argument("--model", default=None, help="defaults to the provider's default_model")
    args = p.parse_args()

    agent = SelfBuildingAgent(workspace=args.workspace,
                                allow_network=args.allow_network, allow_spawn=args.allow_spawn,
                                provider=args.provider, model=args.model)
    try:
        model = agent._client()      # fails fast on a missing key or bad provider
    except Exception as e:
        sys.exit(f"arcllm could not load provider '{args.provider}': {e}")
    if os.path.exists(args.state):
        agent.load(args.state)
        print(f"[loaded: {len(agent.manifest)} tools, team of {len(agent.team)}, "
              f"identity {len(agent.identity)} chars]")
    else:
        print("[cold start]")
    print(f"[{args.provider} · {model.model_name}]")
    print("[!secret NAME | !secrets | !fb <text> | review | new | tools | skills | skill <name> "
          "| team | identity | raw | history | cost | exit]\n")

    while True:
        try:
            task = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not task:
            continue
        low = task.lower()
        if low in ("exit", "quit"):
            break
        if low.startswith("!secret "):
            name = task.split(None, 1)[1].strip().upper().replace(" ", "_")
            agent.secret_store(name, getpass.getpass(f"value for {name} (hidden, local-only): "))
            print(f"[stored {name} — grown code reads it via self._secret('{name}')]")
            continue
        if low == "!secrets":
            print("stored:", ", ".join(agent.secret_names()) or "(none)")
            continue
        if low.startswith("!fb "):
            print(agent.feedback(task[4:].strip())["output"])
            agent.save(args.state)
            continue
        if low in ("new", "clear"):
            agent.messages = []
            agent.save(args.state)
            print("  [conversation cleared — tools, skills and memory kept]")
            continue
        if low == "review":
            print(agent.review()["output"])
            agent.save(args.state)
            continue
        if low == "history":
            print(agent._trace_digest(40))
            continue
        if low == "cost":
            print(f"  ${agent.cost_usd:.4f} this session")
            continue
        if low == "tools":
            for n in sorted(agent.manifest):
                owner = next((sn for sn, s in agent.team.items() if n in s["tools"]), None)
                print(f"  - {n}" + (f"  [owned by {owner}]" if owner else ""))
            continue
        if low == "skills":
            assigned_s = agent._assigned_skills()
            for n, s in agent.skills.items():
                owner = next((sn for sn, sp in agent.team.items()
                              if n in sp.get("skills", [])), None)
                print(f"  - {n}: {s['when']} ({len(s['body'])} chars, read {s.get('uses', 0)}x)"
                      + (f"  [owned by {owner}]" if owner else ""))
            if not agent.skills:
                print("  (no skills written yet)")
            continue
        if low.startswith("skill "):
            print(agent.read_skill(task.split(None, 1)[1].strip()))
            continue
        if low == "team":
            for n, s in agent.team.items():
                print(f"  - {n}: {s['description']} ({len(s['tools'])} tools)")
            if not agent.team:
                print("  (no specialists yet)")
            continue
        if low == "identity":
            print(agent._build_system())
            continue
        if low == "raw":
            print(agent._raw_read() or "(empty)")
            continue
        try:
            print(agent.run(task)["output"])
        except Exception as e:
            # A provider error should cost you one turn, not the session.
            print(f"[call failed: {type(e).__name__}: {str(e)[:300]}]")
            if "not_found" in str(e) or "404" in str(e):
                print(f"[the model '{model.model_name}' is unknown to this provider — "
                      f"try --model <id>, or upgrade arcllm for current defaults]")
        agent.save(args.state)

    agent.save(args.state)
    _sync(model.close())
    print(f"\n[saved to {args.state} · ${agent.cost_usd:.4f} spent]")


if __name__ == "__main__":
    _main()

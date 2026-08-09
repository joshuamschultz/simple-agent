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

The model lives behind llm.py — one method, plain dicts, no provider types
in this file. Swap that file for litellm, a raw client, anything, and
nothing here changes.

Run:  export ANTHROPIC_API_KEY=...  &&  python3 agent.py
Sandbox note: subprocess isolation stops accidents (hangs, crashes, casual
vault access), not malice — same user, same filesystem. Container-wrap the
identical runner before real stakes.
"""

from __future__ import annotations
import inspect
import json
import os
import re as _re
import shutil
import stat
import subprocess
import sys
import textwrap
import time
import types
import uuid

_HERE = os.path.dirname(os.path.abspath(__file__))

# The only thing this file knows about models: whether the seam imports.
# If it doesn't, llm.py decides what to do about it.
try:
    from llm import LLM, bootstrap, venv_handoff
    import prompts as _prompts_probe          # noqa: F401
except ImportError:
    try:
        from llm import venv_handoff          # deps missing, seam still importable
        venv_handoff(__file__)
    except ImportError:                       # the seam itself can't load
        _venv = os.path.join(_HERE, ".venv", "bin", "python")
        if os.path.realpath(sys.argv[0] or "") == os.path.realpath(__file__) \
                and os.path.exists(_venv) \
                and os.path.realpath(sys.prefix) != os.path.realpath(os.path.join(_HERE, ".venv")):
            os.execv(_venv, [_venv, os.path.abspath(__file__), *sys.argv[1:]])
        sys.exit("Not set up yet. Run ./setup.sh once, then `python3 agent.py`.")

from prompts import DEFAULT_PROMPTS  # noqa: E402

bootstrap(__file__)

MAX_TOKENS = 50000         # a cap, not a charge — only real output tokens bill
MAX_TOOL_ITERS = 24
CONTEXT_MESSAGES = 60      # running conversation kept in front of the model
TOOL_SUMMARY_CHARS = 220   # of a grown tool's docstring, what rides in every turn
MAX_SPAWN_DEPTH = 2
REVIEW_EVERY = 5                # review often; patterns hide in the gaps
REVIEW_WINDOW = 50              # but always look back further than you review
CORRECTIONS_BEFORE_REVIEW = 2   # or sooner, when the user has had to correct you
SANDBOX_TIMEOUT = 30
MAX_QUESTIONS_PER_TASK = 3  # steering, not an interview. Past this it decides
                            # for itself and says what it assumed.
TRACES_IN_STATE = 200      # older traces live in the append-only log, not state
SURVEY_CHUNK = 400000      # characters handed to one reader ~= 100k tokens. Small
                           # enough for a 200k window, a rounding error in a 1M
                           # one. Sized so that splitting is the rare case: one
                           # reader that sees everything beats five that don't.

# Every harness-owned failure string starts with this. A tool result either
# begins with FAILED: or it worked — that is the whole outcome contract, and
# it is why traces can record success without parsing anything.
FAILED = "FAILED: "

_JSON_TYPE = {str: "string", int: "integer", float: "number", bool: "boolean",
              list: "array", dict: "object"}
# `from __future__ import annotations` hands every annotation over as its NAME,
# so a lever declared `tools: list` would be advertised as a string — and then
# handed one. Both spellings mean the same JSON type.
_JSON_TYPE.update({t.__name__: kind for t, kind in list(_JSON_TYPE.items())})

_RUNNER = '''
import json, os, sys
class AgentShim:
    def _raw_read(self):
        try:
            with open("memory.data") as f: return f.read()
        except FileNotFoundError: return ""
    def _raw_write(self, content):
        if not isinstance(content, str): content = json.dumps(content)
        with open("memory.data.tmp", "w") as f: f.write(content)
        os.replace("memory.data.tmp", "memory.data")
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
                   "forget_tool", "update_identity", "read_prompt", "update_prompt",
                   "create_specialist",
                   "call_specialist", "dissolve_specialist", "spawn_agent", "survey",
                   "ask_user")
    _META_CHILD = ("grow_tool", "read_tool", "grow_skill", "read_skill",
                    "update_identity", "spawn_agent", "survey", "ask_user")

    def __init__(self, client=None, workspace: str = "agent_workspace",
                 allow_network: bool = False, allow_spawn: bool = False,
                 provider: str = "anthropic", model: str | None = None):
        self.client = client          # an llm.LLM; built lazily if None
        self.provider = provider
        self.model = model            # None -> provider's default_model
        self.cost_usd = 0.0
        self.model_calls = 0          # how many round trips, and how long they took —
        self.model_seconds = 0.0      # the only honest way to locate a slow turn
        self.workspace = workspace
        os.makedirs(workspace, exist_ok=True)
        self.prompts: dict[str, str] = dict(DEFAULT_PROMPTS)
        self.manifest: dict[str, str] = {}          # ALL grown tools, central
        self.skills: dict[str, dict] = {}           # name -> {when, body, uses}
        self.team: dict[str, dict] = {}             # name -> {identity, tools, skills, description}
        self.traces: list[dict] = []
        self.messages: list[dict] = []         # the live conversation
        self.on_event = None        # optional observer; the REPL uses it to narrate
        self.ask_human = None       # optional questioner; set only when a person
                                    # is at the terminal. None -> ask_user is not
                                    # offered at all, and it decides for itself.
        self.allow_network = allow_network
        self.allow_spawn = allow_spawn
        self.tasks_since_maintenance = 0
        self.corrections_since_review = 0
        # A one-element list because every child shares this exact object: the
        # budget belongs to the TASK, not to whoever happens to be working on
        # it. Three questions from the root and three more from a specialist it
        # handed off to is six questions, and six is an interview.
        self._asked = [0]
        self._fresh_turn = False
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
        """Write the store atomically: serialize, write a temp file, rename
        over the original. A failure leaves the previous contents intact."""
        if not isinstance(content, str):
            content = json.dumps(content)
        path = os.path.join(self.workspace, "memory.data")
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write(content)
        os.replace(tmp, path)

    # ---------------- sandboxed execution ----------------

    @staticmethod
    def _code(entry) -> str:
        """A manifest entry is {'code', 'description'}. Older states stored
        the code string alone; both are read through here."""
        return entry["code"] if isinstance(entry, dict) else entry

    @staticmethod
    def _describe(entry) -> str:
        if isinstance(entry, dict) and entry.get("description"):
            return entry["description"]
        m = _re.search(r'"""(.*?)"""', SelfBuildingAgent._code(entry), _re.S)
        return " ".join(m.group(1).split()) if m else ""

    def _validate(self, code: str, fn_name: str) -> bool:
        try:
            compile(code, "<grown>", "exec")
        except SyntaxError:
            return False
        return f"def {fn_name}(" in code

    def _run_sandboxed(self, manifest: dict, fn_name: str, kwargs: dict) -> str:
        runner = _RUNNER.format(
            manifest_code="\n\n".join(self._code(e) for e in manifest.values()), fn_name=fn_name)
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

    # ---------------- the model ----------------

    def _absorb(self, child: "SelfBuildingAgent") -> None:
        """Take back everything a finished child produced.

        Not just what it spent — what it BUILT. A tool a reader had to write
        to get through its part is as real as one you wrote yourself, and
        discarding it only means the next child writes it again. Every path
        that runs a child goes through here, so growth is kept the same way
        whoever did the growing.

        A child never clobbers a tool you already have (its copy may be a
        stale fork of yours); it does hand back skills, which is how a
        specialist sharpens shared judgment.
        """
        self.cost_usd += child.cost_usd
        self.model_calls += child.model_calls
        self.model_seconds += child.model_seconds
        for name, entry in child.manifest.items():
            self.manifest.setdefault(name, entry)
        self.skills.update(child.skills)

    def _run_child(self, child: "SelfBuildingAgent", task: str) -> str:
        """Run a child to completion and take back everything it produced."""
        result = child.run(task)
        self._absorb(child)
        return result["output"]

    def _child(self, identity: str, workspace: str | None = None,
               tools: list | None = None, skills: list | None = None) -> "SelfBuildingAgent":
        """The one way a child agent is born, used by every path that makes
        one — a specialist, a one-off spawn, a survey reader.

        Same connection pool, same harness-owned vault, same narration, one
        level deeper. Only three things ever differ: who it is, where it
        works, and which subset of the registry it can see. None is a subset
        meaning "all of it".
        """
        child = SelfBuildingAgent(client=self._client(),
                                  workspace=workspace or self.workspace,
                                  allow_network=self.allow_network,
                                  allow_spawn=self.allow_spawn,
                                  provider=self.provider, model=self.model)
        child._vault_path = self._vault_path      # one vault, harness-owned
        child._spawn_depth = self._spawn_depth + 1
        child.on_event = self.on_event            # its work narrates under yours
        child.ask_human = self.ask_human          # and it asks the same person,
        child._asked = self._asked                # out of the same budget
        child.manifest = (dict(self.manifest) if tools is None else
                          {n: self.manifest[n] for n in tools if n in self.manifest})
        child.skills = (dict(self.skills) if skills is None else
                        {n: self.skills[n] for n in skills if n in self.skills})
        child.identity = identity
        return child

    def _client(self):
        """Long-lived — one connection pool, shared with every specialist
        and sub-agent this agent creates."""
        if self.client is None:
            self.client = LLM(self.provider, self.model, label=f"selfbuilder:{self.workspace}")
        return self.client

    def _complete(self, system: str, messages: list[dict], tools: list[dict] | None = None) -> dict:
        started = time.perf_counter()
        try:
            reply = self._client().complete(system, messages, tools, max_tokens=MAX_TOKENS)
        finally:
            # Count the wait even when the call raises — a slow failure is
            # still where the time went.
            self.model_seconds += time.perf_counter() - started
            self.model_calls += 1
        self.cost_usd += reply["cost_usd"] or 0.0
        return reply

    @staticmethod
    def _summary(doc: str) -> str:
        """First paragraph of a docstring, capped, for the tool schema.

        Same contract as a skill: the summary is always loaded, the full text
        loads on demand via read_tool. Model-authored docstrings have no
        length limit, so the cap is what keeps the schema small."""
        first = doc.strip().split("\n\n")[0].strip()
        first = " ".join(first.split())
        if len(first) <= TOOL_SUMMARY_CHARS:
            return first
        cut = first[:TOOL_SUMMARY_CHARS]
        stop = max(cut.rfind(". "), cut.rfind("; "))
        return (cut[:stop + 1] if stop > TOOL_SUMMARY_CHARS // 2 else cut.rstrip() + "...") \
            + " [read_tool for the full contract]"

    def _tool_contract(self, name: str, entry) -> str:
        """A tool as a caller needs it: how to call it, what it promises.
        Never its body — that is what read_tool is for."""
        sig = self._code(entry).split("\n", 1)[0].strip()
        sig = sig[4:-1] if sig.startswith("def ") and sig.endswith(":") else name
        doc = self._summary(self._describe(entry))
        return f"- self.{sig}\n    {doc or '(no description)'}"

    def _draft_method(self, gap_description: str) -> dict:
        import re as _re
        # Contracts, not source: a caller needs the interface, and seeing the
        # existing implementation only anchors the new tool to its shape.
        existing = "\n".join(self._tool_contract(n, c) for n, c in self.manifest.items()) or "(none yet)"
        network = ("You may use urllib/sockets and any installed package to call APIs — network is enabled."
                   if self.allow_network else "No network calls.")
        prompt = self.prompts["draft"].format(
            gap_description=gap_description, existing=existing, network=network,
            TOOL_SUMMARY_CHARS=TOOL_SUMMARY_CHARS)
        reply = self._complete("", [{"role": "user", "content": prompt}])
        cleaned = _re.sub(r"^```(?:json)?|```$", "", (reply["content"] or "").strip(),
                          flags=_re.MULTILINE).strip()
        spec = json.loads(cleaned)
        spec.setdefault("description", self._describe(spec.get("code", "")))
        return spec

    # ---------------- meta-tools (the model's levers) ----------------

    def grow_tool(self, description: str) -> str:
        """Draft a new Python method from a concrete description (inputs,
        output, behavior) and make it callable on your next step."""
        try:
            spec = self._draft_method(description)
            if not self._validate(spec["code"], spec["name"]):
                return f"{FAILED}draft did not validate — try a more concrete description."
            self.manifest[spec["name"]] = {
                "code": spec["code"],
                "description": spec.get("description") or self._describe(spec["code"])}
            return f"Grew {spec['name']}. Callable from your next step."
        except Exception as e:
            return f"{FAILED}draft error: {e}"

    def read_tool(self, name: str) -> str:
        """Read a grown tool's source. Do this before changing how something
        is stored or retrieved — grow_tool with the same name REPLACES it, so
        repairing the tool you have beats growing a second one beside it."""
        entry = self.manifest.get(name)
        if not entry:
            return f"{FAILED}no tool named '{name}'. You have: {list(self.manifest)}"
        return f"{name} — {self._describe(entry)}\n\n{self._code(entry)}"

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

    def survey(self, question: str, source: str = "memory") -> str:
        """Answer one question about something FAR too long to read at once.
        Hands each part of the source to its own reader — a fresh agent with a
        clean window that sees nothing but that part and your question — then
        combines what they found. Nothing large ever enters your own context.
        source is "memory" for the entire raw store, "state" for everything you
        have built, or a file path. A tool shows you only its own namespace;
        this shows you all of it, including records left under keys no current
        tool owns."""
        try:
            text = self._source_text(source)
        except OSError as e:
            return f"{FAILED}cannot read '{source}': {e}"
        if not text.strip():
            return f"'{source}' is empty."

        parts = [text[i:i + SURVEY_CHUNK] for i in range(0, len(text), SURVEY_CHUNK)]
        found = []
        for i, part in enumerate(parts, 1):
            said = self._read_part(
                self.prompts["survey"].format(part=i, parts=len(parts), question=question),
                part)
            if said and said.lower().rstrip(".") != "nothing here":
                found.append(said)
        if not found:
            return (f"Read all {len(text):,} characters of {source}, in "
                    f"{len(parts)} part(s). Nothing in it answers that.")
        if len(parts) == 1:
            return found[0]

        merged = "\n\n".join(found)
        # Combining is one more read, so it obeys the same limit. Folding the
        # findings again would silently drop records, and a quiet wrong answer
        # is worse than a loud refusal — say so and let the caller narrow it.
        if len(merged) > SURVEY_CHUNK:
            return (f"{FAILED}{len(parts)} readers came back with {len(merged):,} "
                    f"characters of findings, more than one reader can hold. Ask "
                    f"something narrower, or survey a smaller source.")
        reply = self._complete(
            self.prompts["merge"].format(question=question, part=1, parts=1),
            [{"role": "user", "content": merged}])
        return (reply["content"] or merged).strip()

    def _read_part(self, identity: str, part: str) -> str:
        """One reader, one part, its own window.

        A real child agent while there is spawn depth left, so a reader can
        call a tool or open a file the part points at rather than only
        describing it. Past that depth it degrades to a plain model call,
        which is what bounds the recursion.
        """
        if self._spawn_depth >= MAX_SPAWN_DEPTH:
            reply = self._complete(identity, [{"role": "user", "content": part}])
            return (reply["content"] or "").strip()
        reader = self._child(identity)   # same world, its own window
        return (self._run_child(reader, part) or "").strip()

    def _source_text(self, source: str) -> str:
        """The three places a long string lives here: the shared store, the
        record of everything grown, or a file on disk."""
        if source == "memory":
            return self._raw_read()
        if source == "state":
            return json.dumps({"manifest": self.manifest, "skills": self.skills,
                               "prompts": self.prompts, "team": self.team}, indent=2)
        with open(source) as f:
            return f.read()

    def read_skill(self, name: str) -> str:
        """Load one skill's full text into this turn. Read a skill BEFORE
        doing the work it covers, not after."""
        skill = self.skills.get(name)
        if not skill:
            return f"{FAILED}no skill named '{name}'. Index: {list(self.skills.keys())}"
        skill["uses"] = skill.get("uses", 0) + 1
        return f"SKILL {name} — {skill['when']}\n\n{skill['body']}"

    def forget_tool(self, name: str) -> str:
        """Delete a tool that should not exist — a one-off you kept, or one
        superseded by something better under a different name. Prefer
        REPLACING it: grow_tool under the same name supersedes it and leaves
        every caller working. Delete only when nothing should own this job.

        If it owned records in the store, MOVE THEM FIRST. Deleting the only
        tool that reads a key does not delete the records — it makes them
        unreachable, which is worse than either keeping the tool or dropping
        the data."""
        if name not in self.manifest:
            return f"{FAILED}no tool named '{name}'. You have: {list(self.manifest)}"
        del self.manifest[name]
        freed = self._unassign("tools", name)
        return (f"Forgot tool '{name}'.{freed} If it owned anything in the store, "
                f"survey memory now and move those records somewhere a tool can reach.")

    def forget_skill(self, name: str) -> str:
        """Delete a skill that is wrong, stale, or superseded. Prefer
        rewriting with grow_skill; delete only when it should not exist."""
        if name not in self.skills:
            return f"{FAILED}no skill named '{name}'."
        del self.skills[name]
        return f"Forgot skill '{name}'.{self._unassign('skills', name)}"

    def _unassign(self, field: str, name: str) -> str:
        """Drop a deleted name from every specialist that listed it, so no
        roster points at something that is no longer there."""
        owners = [sn for sn, spec in self.team.items() if name in spec.get(field, ())]
        for sn in owners:
            self.team[sn][field] = [x for x in self.team[sn][field] if x != name]
        return f" Also removed from {', '.join(owners)}." if owners else ""

    @property
    def identity(self) -> str:
        return self.prompts["identity"]

    @identity.setter
    def identity(self, value: str) -> None:
        self.prompts["identity"] = value

    def update_identity(self, new_identity: str) -> str:
        """Replace your entire system prompt. Full replacement, not append —
        carry forward what still matters. Takes effect next turn."""
        self.prompts["identity"] = new_identity
        return f"Identity updated ({len(new_identity)} chars)."

    def read_prompt(self, name: str) -> str:
        """Read one of the harness prompts that drives you: 'identity' (your
        system prompt), 'draft' (how new tools get written), 'review' (how you
        review your own work). Read before rewriting."""
        if name not in self.prompts:
            return f"{FAILED}no prompt named '{name}'. You have: {list(self.prompts)}"
        return self.prompts[name]

    def update_prompt(self, name: str, text: str) -> str:
        """Rewrite one of the harness prompts. This is the deepest lever you
        have: 'draft' decides how every future tool gets written, 'review'
        decides how you judge your own work. Sharpen them when the review
        shows they produced a bad tool or missed a real problem. Placeholders
        in the original must survive, or the prompt cannot be filled in."""
        if name not in self.prompts:
            return f"{FAILED}no prompt named '{name}'. You have: {list(self.prompts)}"
        missing = [ph for ph in _re.findall(r"\{(\w+)\}", DEFAULT_PROMPTS.get(name, ""))
                   if "{" + ph + "}" not in text]
        if missing:
            return (f"{FAILED}your version drops the placeholders {missing}, which the harness "
                    f"fills in. Keep every one of them and try again.")
        self.prompts[name] = text
        return f"Rewrote the '{name}' prompt ({len(text)} chars). It takes effect immediately."

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
        """Run one task on a registered specialist. Same machinery as a one-off
        sub-agent, with its saved state injected: its own identity, its own
        tool and skill subset, its own memory that has been accumulating since
        the day it was created. What it grows joins the central registry and
        its own roster entry, so it is sharper on the next call."""
        spec = self.team.get(name)
        if not spec:
            return f"{FAILED}no specialist named '{name}'. Roster: {list(self.team.keys())}"
        child = self._child(spec["identity"],
                            os.path.join(self.workspace, "team", name),
                            spec["tools"], spec.get("skills", []))
        output = self._run_child(child, task)
        # _absorb already put its growth in the central registry. All that is
        # left is which of it this specialist now owns, and who it has become.
        spec["tools"] = list(child.manifest)
        spec["skills"] = list(child.skills)
        spec["identity"] = child.identity
        return output

    def dissolve_specialist(self, name: str) -> str:
        """Remove a stale specialist from the roster. Its tools stay in the
        central registry and return to your own schema; its memory file is
        kept on disk in case it's re-created later."""
        if name not in self.team:
            return f"{FAILED}no specialist named '{name}'."
        del self.team[name]
        return (f"Dissolved '{name}'. Its tools and skills are back in your context; "
                f"its memory remains on disk.")

    def spawn_agent(self, identity: str, task: str, tools: list = None,
                    seed_memory: str = "") -> str:
        """One-off sub-agent: injected prompt, optional tool subset (all yours
        if unspecified), optional seed memory. Runs one task and is wound
        down — but anything it had to build to finish gets kept, the same as
        if you had built it."""
        if self._spawn_depth >= MAX_SPAWN_DEPTH:
            return f"{FAILED}max recursion depth reached."
        names = tools if tools else list(self.manifest.keys())
        child = self._child(identity,
                            os.path.join(self.workspace, f"adhoc_{uuid.uuid4().hex[:6]}"),
                            names)
        if seed_memory:
            child._raw_write(seed_memory)
        grown = [n for n in child.manifest if n not in names]
        output = self._run_child(child, task)
        return output + (f" [it also built, and you kept: {', '.join(grown)}]" if grown else "")

    @staticmethod
    def _options(options: list) -> list[tuple]:
        """Options as (label, description). A plain string is a label with no
        description; a dict may carry both. A whole list arriving as one JSON
        string is unpacked — that is a normal thing for a model to send."""
        if isinstance(options, str):
            try:
                options = json.loads(options)
            except json.JSONDecodeError:
                options = [options]
        out = []
        for opt in options or []:
            if isinstance(opt, dict):
                label = str(opt.get("label") or opt.get("option") or "").strip()
                detail = str(opt.get("description") or opt.get("detail") or "").strip()
            else:
                label, detail = str(opt).strip(), ""
            if label:
                out.append((label, detail))
        return out

    def ask_user(self, question: str, options: list, header: str = "",
                 allow_multiple: bool = False) -> str:
        """Put a choice to the person and wait — they pick in their terminal and
        their answer comes back to you as this tool's result.

        Ask when the choice is genuinely theirs AND the answers lead to
        different work: which of two readings of a vague request is meant, what
        shape their data should take, how strict a rule should be, what to build
        next. Ask BEFORE you build, not after — a question costs seconds, the
        wrong build costs the whole turn.

        Do not ask what you could look up, what has one sensible answer, or for
        permission to do the thing you were just asked to do. Two or three per
        task at most; past that, decide and say what you assumed.

        options: 2-4 entries, each {"label": "the choice, a few words",
        "description": "what it means or costs them"} — a plain string works
        too. Put the one you recommend first. An escape hatch is added for you,
        so never write one yourself. header: 2-4 words naming the decision.
        allow_multiple: true only when picking several really is coherent."""
        if self._fresh_turn:
            return (f"{FAILED}nobody is watching a review. Decide this one yourself "
                    f"and fix what needs fixing.")
        if not self.ask_human:
            return (f"{FAILED}nobody is at the terminal. Take the option you would "
                    f"have recommended, say which you took and why, and carry on.")
        choices = self._options(options)
        if len(choices) < 2:
            return (f"{FAILED}give at least two real options — a question with one "
                    f"answer is a decision you have already made.")
        if self._asked[0] >= MAX_QUESTIONS_PER_TASK:
            return (f"{FAILED}{MAX_QUESTIONS_PER_TASK} questions have already been asked "
                    f"on this task, which is the limit. Decide the rest yourself, state "
                    f"the assumption you made, and let them correct you if it was wrong.")
        self._asked[0] += 1
        try:
            answer = self.ask_human({"question": question, "options": choices,
                                     "header": (header or "one decision").strip(),
                                     "allow_multiple": bool(allow_multiple)})
        except KeyboardInterrupt:
            answer = ""
        except Exception as e:
            return f"{FAILED}could not ask: {type(e).__name__}: {e}"
        if not answer:
            return ("They skipped the question. Decide it yourself, say which way you "
                    "went, and keep going — do not ask this again.")
        return f"They chose: {answer}"

    # ---------------- schemas & system prompt ----------------

    def _meta_names(self):
        """Which levers exist this turn. A lever with nothing behind it is not
        offered at all — asking is only a tool while someone is there to answer,
        and never during a review it gave itself."""
        names = self._META_ROOT if self._spawn_depth == 0 else self._META_CHILD
        return [n for n in names
                if (n != "spawn_agent"
                    or (self.allow_spawn and self._spawn_depth < MAX_SPAWN_DEPTH))
                and (n != "ask_user" or (self.ask_human and not self._fresh_turn))]

    def _schema_for(self, name: str, fn, skip_self=False) -> dict:
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
        if skip_self:                      # a grown tool — model-authored, unbounded
            doc = self._summary(doc)
        return {"name": name, "description": doc,
                "parameters": {"type": "object", "properties": props, "required": required}}

    def _assigned(self, field: str) -> set:
        """Everything the team owns, and so everything the root stops carrying."""
        out = set()
        for spec in self.team.values():
            out.update(spec.get(field, ()))
        return out

    def _tools_schema(self) -> list[dict]:
        schema = [self._schema_for(name, getattr(self, name)) for name in self._meta_names()]
        assigned = self._assigned("tools") if self._spawn_depth == 0 else set()
        for name, entry in self.manifest.items():
            if name in assigned:
                continue  # a specialist owns it — out of the root's context
            try:
                ns: dict = {}
                exec(compile(self._code(entry), "<schema>", "exec"), {"__builtins__": {}}, ns)
                fn = ns.get(name)
                if not fn:
                    continue
                tool = self._schema_for(name, fn, skip_self=True)
                tool["description"] = self._summary(self._describe(entry)) or tool["description"]
                schema.append(tool)
            except Exception:
                continue
        return schema

    def _build_system(self) -> str:
        parts = [self.identity]
        assigned = self._assigned("skills") if self._spawn_depth == 0 else set()
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

    # ---------------- narration ----------------

    def _emit(self, phase: str, tool: str, args: dict | None = None,
              result: str = "", fresh: bool = False) -> None:
        """Tell an observer what the agent is doing, as it does it.

        Two phases per tool call — 'start' before it runs, 'end' after — so a
        watcher can print a handoff line BEFORE the specialist's own activity
        nests underneath it. 'answer' fires once, when a turn produces its
        final reply. Narration is decoration: every failure here is swallowed,
        because a broken display must never cost you a task.
        """
        if not self.on_event:
            return
        try:
            self.on_event({"phase": phase, "tool": tool, "args": args or {},
                           "result": result, "ok": not result.startswith(FAILED),
                           "meta": tool in self._meta_names(),
                           "depth": self._spawn_depth, "fresh": fresh})
        except Exception:
            pass

    # ---------------- the loop ----------------

    def act(self, task: str, fresh: bool = False):
        """One task, inside the running conversation.

        `fresh` isolates meta-work — a review, a draft — so it reasons about
        the session without becoming part of it.
        """
        self._fresh_turn = fresh
        if self._spawn_depth == 0:
            self._asked[0] = 0      # a fresh budget per task, spent by whoever
                                    # ends up doing the work
        if fresh:
            messages: list[dict] = [{"role": "user", "content": task}]
        else:
            self.messages.append({"role": "user", "content": task})
            messages = self.messages
        calls: list[dict] = []
        for _ in range(MAX_TOOL_ITERS):
            reply = self._complete(self._build_system(), messages, self._tools_schema())
            if reply["stop_reason"] != "tool_use":
                # A turn cut off at the token cap has no usable answer; name
                # it so run() records the task as failed.
                if reply["stop_reason"] == "max_tokens" and not reply["content"]:
                    answer = f"[no final answer — hit the {MAX_TOKENS} token cap mid-response]"
                else:
                    answer = reply["content"] or ""
                self._emit("answer", "", result=answer, fresh=fresh)
                if not fresh:
                    messages.append({"role": "assistant", "content": answer or "(no answer)"})
                    self._trim_conversation()
                return answer, calls
            blocks = ([{"type": "text", "text": reply["content"]}] if reply["content"] else []) + [
                {"type": "tool_use", "id": c["id"], "name": c["name"], "arguments": c["arguments"]}
                for c in reply["tool_calls"]]
            messages.append({"role": "assistant", "content": blocks})
            tool_results = []
            meta = set(self._meta_names())
            for call in reply["tool_calls"]:
                name, args = call["name"], call["arguments"]
                self._emit("start", name, args, fresh=fresh)
                try:
                    if name in meta:
                        result = getattr(self, name)(**args)
                    elif name in self.manifest:
                        result = self._run_sandboxed(self.manifest, name, args)
                    else:
                        result = f"{FAILED}no such tool: {name}"
                except Exception as e:
                    result = f"{FAILED}{type(e).__name__}: {e}"
                result = str(result)
                self._emit("end", name, args, result, fresh=fresh)
                calls.append({"tool": name, "ok": not result.startswith(FAILED),
                              "args": args, "result": result[:200]})
                tool_results.append({"type": "tool_result", "tool_use_id": call["id"],
                                      "content": result})
            messages.append({"role": "tool", "content": tool_results})
        if not fresh:
            self._trim_conversation()
        return "[no final answer — exceeded tool-use iteration budget]", calls

    def _trim_conversation(self) -> None:
        """Trim the conversation to CONTEXT_MESSAGES, dropping whole exchanges
        from the front. Cutting mid-exchange would orphan a tool call."""
        while len(self.messages) > CONTEXT_MESSAGES:
            del self.messages[0]
            while self.messages and self.messages[0]["role"] != "user":
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
        """Render the recent session verbatim: each task, the tool calls it
        made with their results, and the answer given. Counts say whether the
        machinery ran; only the transcript shows whether the user was served."""
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

    def _act_review(self):
        """The periodic review: hand the model its own transcript and ask one
        question of it. Runs isolated, so it never joins the conversation."""
        return self.act(
            self.prompts["review"].format(
                REVIEW_WINDOW=REVIEW_WINDOW, transcript=self._session_review()),
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
            # Cadence or signal: corrections pull the review forward.
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
            f"Your recent history, for context:\n{self._session_review(10)}")
        self._record(f"[correction] {correction}", calls, output, True)
        return {"output": output, "calls": calls}

    # ---------------- persistence ----------------

    def save(self, path: str):
        # Only the recent tail of history lives here. The rest is already in
        # workspace/traces.jsonl, so state.json stays a roughly fixed size no
        # matter how many months this agent has been running.
        with open(path, "w") as f:
            json.dump({"manifest": self.manifest, "skills": self.skills,
                        "prompts": self.prompts,      # identity lives in here too
                        "team": self.team,
                        "since_review": self.tasks_since_maintenance,
                        "corrections_since_review": self.corrections_since_review,
                        "traces": self.traces[-TRACES_IN_STATE:]}, f, indent=2)

    def load(self, path: str):
        with open(path) as f:
            data = json.load(f)
        # Lift legacy bare-code entries into {code, description}.
        self.manifest = {}
        for n, entry in data.get("manifest", {}).items():
            code = self._code(entry)
            if self._validate(code, n):
                self.manifest[n] = {"code": code, "description": self._describe(entry)}
        self.skills = data.get("skills", {})
        # Saved prompts win; anything this build added that the state predates
        # is filled in from the defaults, so upgrades are never lost.
        # Seeded from prompts.py, owned by the agent from then on. Defaults
        # only fill keys this state has never seen. Delete state for stock.
        self.prompts = {**DEFAULT_PROMPTS, **data.get("prompts", {})}
        if "identity" in data:          # state written before identity moved in
            self.prompts["identity"] = data["identity"]
        self.team = data.get("team", {})
        self.traces = data.get("traces", [])[-TRACES_IN_STATE:]
        # The conversation is deliberately NOT restored. A restart is a clean
        # slate for the thread; what persists is what was learned — tools,
        # skills, memory, identity.
        self.messages = []
        self.tasks_since_maintenance = data.get("since_review", 0)
        self.corrections_since_review = data.get("corrections_since_review", 0)


# ---------------- narration for the REPL ----------------

# What each lever looks like when you watch it happen. The point is that
# building a tool, writing a note and promoting a specialist are all visibly
# ordinary moves — not hidden machinery.
_ACTIVITY = {
    "grow_tool":           ("✎", "built a tool"),
    "read_tool":           ("↳", "read a tool"),
    "grow_skill":          ("✎", "wrote a note"),
    "read_skill":          ("↳", "read a note"),
    "forget_skill":        ("✗", "dropped a note"),
    "forget_tool":         ("✗", "dropped a tool"),
    "update_identity":     ("⟲", "rewrote itself"),
    "read_prompt":         ("↳", "read a prompt"),
    "survey":              ("⌕", "read all of"),
    "update_prompt":       ("⟲", "rewrote a prompt"),
    "create_specialist":   ("⚑", "new specialist"),
    "call_specialist":     ("→", "handed off to"),
    "dissolve_specialist": ("✗", "dissolved"),
    "spawn_agent":         ("→", "sub-agent"),
}
# These two run a whole other agent. Announce them BEFORE they run, so that
# agent's own activity reads as nested underneath.
_HANDOFF = ("call_specialist", "spawn_agent")

# Colour says WHAT KIND of thing a name is, so a tool never reads as a note.
_ANSI = {"tool": "36", "skill": "32", "prompt": "35", "team": "33",
         "store": "34", "ok": "32", "bad": "31", "dim": "2"}
_KIND = {"grow_tool": "tool", "read_tool": "tool",
         "forget_tool": "tool",
         "grow_skill": "skill", "read_skill": "skill", "forget_skill": "skill",
         "read_prompt": "prompt", "update_prompt": "prompt", "update_identity": "prompt",
         "survey": "store",
         "create_specialist": "team", "call_specialist": "team",
         "dissolve_specialist": "team", "spawn_agent": "team"}


def _painter():
    """Colour only when a human is watching. Pipes and NO_COLOR opt out, so
    redirected output stays free of escape codes."""
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return lambda text, kind: text
    return lambda text, kind: f"\033[{_ANSI[kind]}m{text}\033[0m"


def _cell(text: str, width: int, kind: str, paint) -> str:
    """Paint the text, pad with the PLAIN width — escape codes are invisible
    to the eye but not to str.ljust."""
    return paint(text, kind) + " " * max(0, width - len(text))


def _one_line(text, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _described(result: str) -> str:
    """read_tool and read_skill both answer '<name> — <description>' on line
    one, then the body. The description is the part worth showing."""
    head = (result or "").split("\n", 1)[0]
    return head.split(" — ", 1)[1] if " — " in head else ""


def _columns(tool: str, args: dict, result: str) -> tuple[str, str]:
    """(what it acted on, what that is) — the two right-hand columns."""
    name = _one_line(args.get("name") or "", 22)
    if tool == "grow_tool":
        m = _re.match(r"Grew (\w+)", result)          # the harness names it back
        return (m.group(1) if m else "?"), _one_line(args.get("description"), 44)
    if tool == "grow_skill":
        return name, _one_line(args.get("when_to_use"), 44)
    if tool in ("read_skill", "read_tool"):
        return name, _one_line(_described(result), 44)
    if tool == "survey":
        return _one_line(args.get("source") or "memory", 22), \
            _one_line(args.get("question"), 44)
    if tool == "create_specialist":
        return name, _one_line(args.get("description"), 44)
    if tool == "call_specialist":
        return name, _one_line(args.get("task"), 44)
    if tool == "spawn_agent":
        return "one-off", _one_line(args.get("task"), 44)
    if tool == "update_identity":
        return f"{len(args.get('new_identity') or '')} chars", ""
    return name, ""


def _call_args(args: dict) -> str:
    """A grown tool's call, the way you'd say it out loud. Values get room to
    stay whole — a filename cut in half tells you nothing."""
    vals = [_one_line(v, 26) for v in args.values() if v not in (None, "", [], {})]
    return ", ".join(vals)


def _reporter():
    """Print what the agent is doing, while it does it.

    Returns a callback for SelfBuildingAgent.on_event. Specialists and
    sub-agents share the same callback and report their own depth, so their
    work indents under the handoff that started it.
    """
    was_fresh = [False]
    paint = _painter()

    def report(ev: dict) -> None:
        fresh = ev["fresh"]
        if fresh and not was_fresh[0]:
            print(paint("  ┌── reviewing its own work " + "─" * 44, "dim"), flush=True)
        was_fresh[0] = fresh
        # A rail down the left edge so the review reads as one block, plainly
        # separate from the task you actually asked for.
        pad = "  " + (paint("│ ", "dim") if fresh else "") + "  " * ev["depth"]

        if ev["phase"] == "answer":
            if fresh and ev["depth"] == 0:
                print(paint("  └── done reviewing", "dim") + "\n", flush=True)
                return
            print(f"{pad}{paint('✓ answered', 'ok')}", flush=True)
            # Say it the moment it exists. The maintenance review runs inside
            # the same run() call, and printing after it made a one-call task
            # look like a twenty-call one.
            if ev["depth"] == 0:
                print("\n" + (ev["result"] or "(no answer)") + "\n", flush=True)
            return

        tool, handoff = ev["tool"], ev["tool"] in _HANDOFF
        failed = ev["phase"] == "end" and not ev["ok"]
        if tool == "ask_user" and not failed:
            return              # the question drew its own block, and its own answer
        if not failed and (ev["phase"] == "start") != handoff:
            return                                    # otherwise each call prints once

        if failed:
            mark, verb, subject, kind = "✗", "failed", _one_line(tool, 22), "bad"
            tail = _one_line(ev["result"][len(FAILED):], 44)
        elif ev["meta"]:
            mark, verb = _ACTIVITY.get(tool, ("·", tool))
            subject, tail = _columns(tool, ev["args"], ev["result"])
            kind = _KIND.get(tool, "tool")
        else:
            # A tool it grew, doing its job. What it was called WITH is the
            # readable part; the raw return is usually a wall of JSON.
            mark, verb, subject, kind = "▸", "ran it", _one_line(tool, 22), "tool"
            inputs = _one_line(_call_args(ev["args"]), 26)
            tail = (inputs + " " * max(0, 28 - len(inputs))
                    + paint("→  " + _one_line(ev["result"], 22), "dim"))
        print((f"{pad}{paint(f'{mark} {verb:<17}', 'dim')}"
               f"{_cell(subject, 24, kind, paint)}{tail}").rstrip(), flush=True)

    return report


# ---------------- asking, at the terminal ----------------

_ESCAPE_HATCH = ("Something else", "say it in your own words")


def _read_key() -> str:
    """One keypress, unbuffered. 'up' | 'down' | 'enter' | 'esc' | 'space' |
    the character itself. Raises KeyboardInterrupt on ctrl-C, as usual.

    Raw mode is entered and left around a single read, so a question never
    leaves the terminal in a strange state if the turn dies mid-choice.
    """
    import select, termios, tty
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        # os.read, never sys.stdin.read — a buffered read swallows the rest of
        # an arrow sequence into Python's own buffer, where select cannot see
        # it, and every arrow then arrives as a bare ESC.
        data = os.read(fd, 8)
        if data == b"\x1b" and select.select([fd], [], [], 0.05)[0]:
            data += os.read(fd, 7)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    if data in (b"\x1b[A", b"\x1bOA"):
        return "up"
    if data in (b"\x1b[B", b"\x1bOB"):
        return "down"
    if data == b"\x03":
        raise KeyboardInterrupt
    if not data or data[:1] == b"\x1b":
        return "esc"                      # a bare ESC, or the terminal closed
    ch = data.decode("utf-8", "ignore")[:1]
    return {"\r": "enter", "\n": "enter", " ": "space"}.get(ch, ch)


def _asker():
    """Ask the person one multiple-choice question, in place, and return what
    they said (empty string = they'd rather it decided).

    Returns a callback for SelfBuildingAgent.ask_human. The block redraws over
    itself while they move, then collapses to a single line once they've
    chosen — so the scrollback keeps the decision and not the menu.
    """
    paint = _painter()

    def draw(q, options, cursor, picked, multi, width) -> list[str]:
        head = _one_line(q["header"], 40)
        lines = [paint("  ┌ ", "dim") + paint(head, "team")
                 + paint(" " + "─" * max(0, width - len(head) - 7), "dim")]
        for line in textwrap.wrap(q["question"], max(20, width - 6)) or [""]:
            lines.append(paint("  │ ", "dim") + line)
        lines.append(paint("  │", "dim"))
        labels = [(("[x] " if i in picked else "[ ] ") if multi else "") + label
                  for i, (label, _) in enumerate(options)]
        column = min(max(len(x) for x in labels) + 3, 46)
        room = width - column - 9        # what is left for the descriptions
        for i, ((_, detail), label) in enumerate(zip(options, labels)):
            here = i == cursor
            cell = label + " " * max(2, column - len(label))
            # On a narrow terminal the choices themselves win the space. Half a
            # description is worse than none — it reads as a different reason.
            tail = _one_line(detail, room) if room >= 18 else ""
            lines.append((paint("  │ ", "dim")
                          + paint("❯ " if here else "  ", "ok")
                          + (paint(cell, "team") if here else cell)
                          + (paint(tail, "dim") if tail else "")).rstrip())
        hint = ("↑↓ move · space pick · enter confirm · esc it decides" if multi
                else "↑↓ move · 1-9 jump · enter choose · esc it decides")
        lines.append(paint("  └ " + _one_line(hint, width - 4), "dim"))
        return lines

    def erase(height: int) -> None:
        """Take the menu back off the screen. Once it is answered it is noise,
        and a session full of spent menus is unreadable."""
        sys.stdout.write(f"\033[{height}A" + "\033[2K\n" * height + f"\033[{height}A")
        sys.stdout.flush()

    def note(header: str, answer: str) -> None:
        """What is left behind: one line, the same shape as everything else the
        agent reports doing."""
        print("  " + paint(f"? {'asked':<17}", "dim")
              + _cell(_one_line(answer or "(left to it)", 22), 24, "team", paint)
              + paint(_one_line(header, 44), "dim"), flush=True)

    def choose(q, options, multi, width) -> str:
        cursor, picked, height = 0, set(), 0
        while True:
            lines = draw(q, options, cursor, picked, multi, width)
            sys.stdout.write(("\033[%dA" % height if height else "")
                             + "".join("\033[2K" + ln + "\n" for ln in lines))
            sys.stdout.flush()
            height = len(lines)
            try:
                key = _read_key()
            except KeyboardInterrupt:
                key = "esc"     # the reflex for "stop asking me" is ctrl-C as
                                # often as esc, and it must not cost the turn
            if key in ("up", "down"):
                cursor = (cursor + (1 if key == "down" else -1)) % len(options)
            elif key.isdigit() and 1 <= int(key) <= len(options):
                cursor = int(key) - 1
                if not multi:
                    key = "enter"
            if key == "space" and multi:
                picked.symmetric_difference_update({cursor})
            elif key == "esc":
                erase(height)
                note(q["header"], "")
                return ""
            elif key == "enter":
                chosen = sorted(picked) if multi and picked else [cursor]
                erase(height)
                if chosen == [len(options) - 1] and not multi:      # the escape hatch
                    typed = input("  " + paint("in your own words: ", "dim")).strip()
                    sys.stdout.write("\033[1A\033[2K")              # take the prompt back
                    note(q["header"], typed)
                    return typed
                answer = ", ".join(options[i][0] for i in chosen)
                note(q["header"], answer)
                return answer

    def ask(q: dict) -> str:
        options = list(q["options"]) + ([] if q["allow_multiple"] else [_ESCAPE_HATCH])
        width = min(shutil.get_terminal_size((88, 24)).columns - 2, 96)
        try:
            return choose(q, options, q["allow_multiple"], width)
        except (ImportError, OSError, ValueError):
            # No raw terminal here (Windows, an odd pty). Fall back to the
            # plainest thing that works everywhere.
            print(f"\n  {q['header']}: {q['question']}")
            for i, (label, detail) in enumerate(options, 1):
                print(f"    {i}. {label}" + (f"   — {detail}" if detail else ""))
            typed = input("  number, your own answer, or blank to let it decide: ").strip()
            if typed.isdigit() and 1 <= int(typed) <= len(options):
                return "" if int(typed) == len(options) else options[int(typed) - 1][0]
            return typed

    return ask


# ---------------- REPL ----------------

def _main():
    import argparse, getpass
    p = argparse.ArgumentParser()
    p.add_argument("--allow-network", action="store_true")
    p.add_argument("--allow-spawn", action="store_true")
    p.add_argument("--workspace", default="agent_workspace")
    p.add_argument("--state", default="agent_state.json")
    p.add_argument("--provider", default="anthropic", help="anything llm.py supports")
    p.add_argument("--model", default=None, help="defaults to the provider's default_model")
    p.add_argument("--quiet", action="store_true", help="hide the live activity lines")
    p.add_argument("--no-questions", action="store_true",
                   help="never stop to ask you anything — it decides everything itself")
    args = p.parse_args()

    agent = SelfBuildingAgent(workspace=args.workspace,
                                allow_network=args.allow_network, allow_spawn=args.allow_spawn,
                                provider=args.provider, model=args.model)
    if not args.quiet:
        agent.on_event = _reporter()
    # Only offer the question lever when there is a person on the other end of
    # it. Piped in, or told not to, it is not in the schema at all.
    if not args.no_questions and sys.stdin.isatty():
        agent.ask_human = _asker()
    try:
        model = agent._client()      # fails fast on a missing key or bad provider
    except Exception as e:
        sys.exit(f"could not load provider '{args.provider}': {e}")
    if os.path.exists(args.state):
        agent.load(args.state)
        def count(n, thing):
            return f"{n} {thing}{'' if n == 1 else 's'}"
        print(f"[loaded: {count(len(agent.manifest), 'tool')}, "
              f"{count(len(agent.skills), 'skill')}, team of {len(agent.team)}, "
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
            out = agent.feedback(task[4:].strip())["output"]
            if not agent.on_event:            # the narrator already said it
                print(out)
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
            print(agent._session_review(40))
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
            wall, calls, waiting = time.perf_counter(), agent.model_calls, agent.model_seconds
            answer = agent.run(task)["output"]
            if agent.on_event:
                # The narrator already said the answer, mid-turn. All that's
                # left is the footer: model time vs total time is the whole
                # diagnosis — if they're close, the turn is generation-bound.
                wall = time.perf_counter() - wall
                waiting = agent.model_seconds - waiting
                print(f"  · {agent.model_calls - calls} model calls · {waiting:.1f}s "
                      f"waiting on the model · {wall:.1f}s total")
            else:
                print(answer)
        except Exception as e:
            # A provider error should cost you one turn, not the session.
            print(f"[call failed: {type(e).__name__}: {str(e)[:300]}]")
            if "not_found" in str(e) or "404" in str(e):
                print(f"[the model '{model.model_name}' is unknown to this provider — "
                      f"try --model <id>]")
        agent.save(args.state)

    agent.save(args.state)
    model.close()
    print(f"\n[saved to {args.state} · ${agent.cost_usd:.4f} spent]")


if __name__ == "__main__":
    _main()

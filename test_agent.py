"""The harness, tested without a model.

One file, like the agent. Every test either stubs the model behind a fake
client or needs no model at all, so the whole suite runs offline and free.

    python3 test_agent.py
"""
from __future__ import annotations
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent as A  # noqa: E402


class FakeLLM:
    """Stands in for llm.LLM. Records what it was asked, answers to order."""
    model_name = "fake"

    def __init__(self, answer: str = "NOTE: user is Josh. invoice total 412.50.",
                 fail: bool = False):
        self.answer, self.fail = answer, fail
        self.calls: list[dict] = []

    def complete(self, system, messages, tools=None, max_tokens=0):
        self.calls.append({"system": system, "messages": messages})
        if self.fail:
            raise RuntimeError("provider down")
        return {"content": self.answer, "tool_calls": [], "stop_reason": "end_turn",
                "cost_usd": 0.001}


DOUBLE = {"code": 'def double(self, x: int) -> int:\n    """Double x."""\n    return x * 2',
          "description": "Double x."}


def build(client=None, tools=True) -> A.SelfBuildingAgent:
    a = A.SelfBuildingAgent(client=client or FakeLLM(), workspace=tempfile.mkdtemp())
    if tools:
        a.manifest["double"] = DOUBLE
    return a


def fill(a, n: int) -> None:
    """n exchanges: a user turn, an assistant turn with a tool call, a result."""
    for i in range(n):
        a.messages.append({"role": "user", "content": f"question {i}"})
        a.messages.append({"role": "assistant", "content": [
            {"type": "text", "text": f"thinking {i}"},
            {"type": "tool_use", "id": f"t{i}", "name": "lookup", "arguments": {"k": i}}]})
        a.messages.append({"role": "tool", "content": [
            {"type": "tool_result", "tool_use_id": f"t{i}", "content": f"result {i}"}]})
        a.messages.append({"role": "assistant", "content": f"answer {i}"})


def described(a, name: str) -> str:
    return next(t["description"] for t in a._tools_schema() if t["name"] == name)


# ---------------- compaction ----------------
# What falls out of the window gets summarized, never silently deleted.

def test_overflow_is_summarized_not_dropped():
    fake = FakeLLM()
    a = build(fake)
    fill(a, 30)                      # 120 messages, window is 60
    a._trim_conversation()

    assert len(a.messages) <= A.CONTEXT_MESSAGES, len(a.messages)
    assert a.messages[0]["role"] == "user", "a trim that orphans a tool call"
    assert a.summary, "the dropped exchanges left no summary behind"
    assert "invoice total 412.50" in a._build_system(), "the summary never reaches the model"

    seen = fake.calls[-1]["messages"][0]["content"]
    assert "question 0" in seen and "lookup" in seen and "result 0" in seen, seen[:300]


def test_a_second_compaction_folds_in_the_first():
    fake = FakeLLM()
    a = build(fake)
    fill(a, 30)
    a._trim_conversation()
    fake.answer = "NOTE: user is Josh. invoice total 412.50. later: shipped."
    fill(a, 30)
    a._trim_conversation()
    assert "shipped" in a.summary
    assert "412.50" in fake.calls[-1]["system"], "the running summary was not handed on"


def test_compaction_failure_costs_detail_not_the_turn():
    a = build(FakeLLM(fail=True))
    fill(a, 30)
    a._trim_conversation()           # must not raise
    assert len(a.messages) <= A.CONTEXT_MESSAGES
    assert a.summary, "a failed compaction should still leave a mark"


def test_a_short_conversation_is_left_alone():
    fake = FakeLLM()
    a = build(fake)
    fill(a, 5)
    a._trim_conversation()
    assert len(a.messages) == 20 and not a.summary
    assert not fake.calls, "compacted a conversation that still fits"


def test_a_restart_forgets_the_summary():
    a = build()
    fill(a, 30)
    a._trim_conversation()
    path = os.path.join(a.workspace, "state.json")
    a.save(path)
    b = build()
    b.load(path)
    assert b.messages == [] and not b.summary, "the thread's summary survived a restart"


# ---------------- run_python ----------------
# A scratch path into the same sandbox the grown tools run in.

def test_prints_are_the_return_value():
    assert build().run_python("print(2 + 2)").strip() == "4"


def test_silence_is_explained_not_blank():
    out = build().run_python("x = 1")
    assert "print" in out and not out.startswith(A.FAILED), out


def test_grown_tools_are_in_scope():
    assert build().run_python("print(self.double(21))").strip() == "42"


def test_the_store_is_reachable_both_ways():
    a = build()
    a._raw_write('{"invoices": {"a": 1}}')
    assert "invoices" in a.run_python("print(self._raw_read())")
    a.run_python('self._raw_write(\'{"invoices": {"a": 2}}\')')
    assert '"a": 2' in a._raw_read(), a._raw_read()


def test_loops_beat_round_trips():
    """The whole point: forty tool calls, one turn, one small answer."""
    assert build().run_python("print(sum(self.double(i) for i in range(40)))").strip() == "1560"


def test_a_syntax_error_is_a_FAILED_not_a_crash():
    out = build().run_python("def (:")
    assert out.startswith(A.FAILED) and "Syntax" in out, out


def test_an_exception_is_a_FAILED_that_names_itself():
    out = build().run_python("print(1/0)")
    assert out.startswith(A.FAILED) and "ZeroDivisionError" in out, out


def test_a_runaway_is_killed():
    old = A.SANDBOX_TIMEOUT
    A.SANDBOX_TIMEOUT = 2
    try:
        out = build().run_python("while True: pass")
    finally:
        A.SANDBOX_TIMEOUT = old
    assert out.startswith(A.FAILED) and "timeout" in out, out


def test_secrets_never_come_back():
    a = build()
    a.secret_store("API", "sk-live-12345")
    out = a.run_python('print("key is " + self._secret("API"))')
    assert "sk-live-12345" not in out and "[REDACTED:API]" in out, out


def test_named_tool_calls_still_work():
    """Regression: the schema path and run_python share one sandbox core."""
    a = build()
    assert a._run_sandboxed("double", {"x": 4}).strip() == "8"
    assert a._run_sandboxed("nope", {}).startswith(A.FAILED)


def test_run_python_is_offered_to_root_and_children():
    a = build()
    assert "run_python" in [t["name"] for t in a._tools_schema()]
    assert "run_python" in a._child("a reader")._meta_names(), "survey's readers cannot compute"


# ---------------- the handoff ----------------
# Code and readers are each other's overflow valve.

def test_a_flood_is_saved_not_dumped():
    out = build().run_python('print("x" * 100000)')
    assert len(out) < A.CODE_OUTPUT_CHARS + 500, f"dumped {len(out)} chars into context"
    assert "survey" in out, "the overflow does not point at the tool that can read it"
    path = re.search(r"(\S*out_\w+\.txt)", out)
    assert path, out[-300:]
    with open(path.group(1)) as f:
        assert f.read().count("x") == 100000, "the overflow was clipped, not saved"


def test_a_flood_becomes_a_survey_source():
    class Reader(FakeLLM):
        def __init__(self):
            super().__init__("found: needle-4242")
            self.read = 0

        def complete(self, system, messages, tools=None, max_tokens=0):
            body = messages[-1]["content"]
            if isinstance(body, str):
                self.read += len(body)
            return super().complete(system, messages, tools, max_tokens)

    fake = Reader()
    a = build(fake)
    out = a.run_python('print("x" * 450000 + "needle-4242")')
    path = re.search(r"(\S*out_\w+\.txt)", out).group(1)
    answer = a.survey("where is the needle", path)
    assert "needle-4242" in answer, answer
    assert fake.read >= 450000, f"survey only read {fake.read:,} of the saved output"


def test_both_reads_reach_the_same_store():
    a = build()
    a._raw_write('{"invoices": {"a": 1}}')
    assert "invoices" in a._source_text("memory")
    assert "invoices" in a.run_python("print(self._raw_read())")


# ---------------- where the words live ----------------
# A prompt is text the harness SENDS: prompts.py, then state, agent-editable.
# A tool description is documentation the schema carries: it stays in code.

def test_the_carried_summary_is_a_prompt():
    a = build()
    a.summary = "user is Josh. invoice total 412.50."
    assert "invoice total 412.50" in a._build_system()
    assert a.prompts["carried"].split("{summary}")[0].strip()[:20] in a._build_system()


def test_the_agent_can_rewrite_it():
    a = build()
    a.summary = "user is Josh."
    assert not a.update_prompt("carried", "OLD NOTES:\n{summary}").startswith(A.FAILED)
    assert a._build_system().endswith("OLD NOTES:\nuser is Josh.")


def test_its_placeholder_is_guarded():
    out = build().update_prompt("carried", "no placeholder")
    assert out.startswith(A.FAILED) and "summary" in out, out


def test_a_prompt_rewrite_persists():
    a = build()
    a.update_prompt("carried", "OLD NOTES:\n{summary}")
    path = os.path.join(a.workspace, "state.json")
    a.save(path)
    b = build()
    b.load(path)
    assert b.prompts["carried"] == "OLD NOTES:\n{summary}"


def test_tool_descriptions_stay_in_code():
    a = build()
    assert not [k for k in a.prompts if k.startswith("tool.")], "tool text leaked into state"
    assert a.update_prompt("tool.survey", "anything").startswith(A.FAILED)
    assert described(a, "survey").startswith("Answer one question")


def test_every_prompt_the_harness_fills_in_is_seeded():
    """A missing prompt is not a crash, it is an agent quietly told less."""
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent.py")) as f:
        used = set(re.findall(r'self\.prompts\[\s*"(\w+)"\s*\]', f.read()))
    missing = used - set(A.DEFAULT_PROMPTS)
    assert not missing, f"agent.py fills in {missing}, prompts.py does not have it"


def test_the_setup_seed_matches():
    """setup.sh regenerates prompts.py, so the seed there is the real source."""
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "setup.sh")) as f:
        lines = f.read().split("\n")
    start = next(i for i, l in enumerate(lines) if l.startswith("cat > prompts.py <<"))
    end = next(i for i, l in enumerate(lines[start + 1:], start + 1) if l.strip() == "PROMPTS_PY_EOF")

    seeded = tempfile.mkdtemp()
    with open(os.path.join(seeded, "prompts.py"), "w") as f:
        f.write("\n".join(lines[start + 1:end]))
    scope: dict = {}
    with open(os.path.join(seeded, "prompts.py")) as f:
        exec(compile(f.read(), "<seed>", "exec"), scope)
    for key in ("identity", "draft", "survey", "merge", "compact", "carried", "review"):
        assert key in scope["DEFAULT_PROMPTS"], f"setup.sh does not seed '{key}'"


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ok    {name[5:].replace('_', ' ')}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {name[5:].replace('_', ' ')}\n        {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)

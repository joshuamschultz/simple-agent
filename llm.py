"""The LLM seam.

agent.py never imports a provider library. It builds plain dicts and calls
one method:

    llm.complete(system, messages, tools, max_tokens) -> dict

Swapping this file for litellm, a raw HTTP client, or anything else is the
whole porting job. Nothing above it changes.

The canonical shapes, all plain JSON-able dicts:

    message  {"role": "user"|"assistant"|"tool", "content": str | [block]}
    block    {"type": "text",        "text": str}
             {"type": "tool_use",    "id": str, "name": str, "arguments": dict}
             {"type": "tool_result", "tool_use_id": str, "content": str}
    tool     {"name": str, "description": str, "parameters": <json schema>}

    reply    {"content": str|None,
              "tool_calls": [{"id": str, "name": str, "arguments": dict}],
              "stop_reason": "end_turn"|"tool_use"|"max_tokens"|...,
              "cost_usd": float|None}

This implementation is arcllm: one provider-agnostic client, direct HTTP, no
vendor SDK. Which provider it talks to is a config question (arcllm.toml),
not a code question — that is the reason it is the default. Retry, cost
telemetry, prompt caching, PII redaction and audit are its modules, toggled
at load_model().
"""

from __future__ import annotations

import asyncio
import inspect
import os

# Imported at module scope ON PURPOSE. agent.py decides whether it can run by
# trying to import this module; a lazily-imported backend would let that probe
# pass and then fail at the first model call instead.
from dotenv import load_dotenv
from arcllm import (Message, TextBlock, Tool, ToolResultBlock, ToolUseBlock,
                    load_model)


def bootstrap(entry_file: str) -> None:
    """Everything that must happen before the first model call.

    Called by agent.py the moment this module imports cleanly. Lives here
    because it is all seam business: which env file wins, and nothing above
    this line needs to know about any of it.
    """
    load_env(os.path.dirname(os.path.abspath(entry_file)))


def venv_handoff(entry_file: str) -> None:
    """Re-exec the entry script under the project venv, then give up.

    agent.py calls this when importing this module failed, so a plain
    `python3 agent.py` works from any shell whose interpreter cannot see the
    backend. Only when the script is the entrypoint — re-execing during an
    `import agent` would replace the caller's program.
    """
    import sys
    here = os.path.dirname(os.path.abspath(entry_file))
    venv_dir = os.path.join(here, ".venv")
    venv_py = os.path.join(venv_dir, "bin", "python")
    entry = os.path.realpath(sys.argv[0] or "") == os.path.realpath(entry_file)
    # Compare prefixes, not interpreter paths: a uv venv's `python` is a
    # symlink to the base interpreter, so realpath() would call them equal.
    if entry and os.path.exists(venv_py) and \
            os.path.realpath(sys.prefix) != os.path.realpath(venv_dir):
        os.execv(venv_py, [venv_py, os.path.abspath(entry_file), *sys.argv[1:]])
    sys.exit("Not set up yet. Run ./setup.sh once, then `python3 agent.py`.")


def load_env(project_dir: str) -> None:
    """Load the project's .env, overriding the shell.

    A stale or mislabelled key exported by the shell otherwise 401s
    silently, and with a fallback chain configured, another provider
    answers in its place. Correct answers, wrong model, no error.
    """
    load_dotenv(os.path.join(project_dir, ".env"), override=True)


_LOOP: asyncio.AbstractEventLoop | None = None


def _sync(coro):
    """Run one coroutine from synchronous code.

    A single long-lived loop, not asyncio.run() per call — an httpx
    connection pool is bound to the loop that created it.
    """
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
    return _LOOP.run_until_complete(coro)


class LLM:
    """One long-lived model client. Create once, call many times."""

    def __init__(self, provider: str = "anthropic", model: str | None = None,
                 label: str | None = None):
        opts = {"telemetry": True,   # per-call USD cost
                "retry": True}       # backoff on 429/5xx
        # agent_label tags traces per agent; older arcllm releases don't take
        # it, and it isn't worth pinning a version over.
        if label and "agent_label" in inspect.signature(load_model).parameters:
            opts["agent_label"] = label
        self._model = load_model(provider, model, **opts)
        self.provider = provider

    @property
    def model_name(self) -> str:
        return self._model.model_name

    # ---- dict -> arcllm ----

    @staticmethod
    def _to_blocks(content):
        if isinstance(content, str):
            return content
        out = []
        for b in content:
            if b["type"] == "text":
                out.append(TextBlock(text=b["text"]))
            elif b["type"] == "tool_use":
                out.append(ToolUseBlock(id=b["id"], name=b["name"], arguments=b["arguments"]))
            elif b["type"] == "tool_result":
                out.append(ToolResultBlock(tool_use_id=b["tool_use_id"], content=b["content"]))
        return out

    def _to_messages(self, system: str, messages: list[dict]):
        out = [Message(role="system", content=system)] if system else []
        for m in messages:
            out.append(Message(role=m["role"], content=self._to_blocks(m["content"])))
        return out

    @staticmethod
    def _to_tools(tools: list[dict] | None):
        if not tools:
            return None
        return [Tool(name=t["name"], description=t["description"],
                     parameters=t["parameters"]) for t in tools]

    # ---- the one method agent.py calls ----

    def complete(self, system: str, messages: list[dict], tools: list[dict] | None = None,
                 max_tokens: int = 8000) -> dict:
        resp = _sync(self._model.invoke(
            self._to_messages(system, messages), self._to_tools(tools), max_tokens=max_tokens))
        return {
            "content": resp.content,
            "tool_calls": [{"id": c.id, "name": c.name, "arguments": c.arguments}
                           for c in resp.tool_calls],
            "stop_reason": resp.stop_reason,
            "cost_usd": resp.cost_usd,
        }

    def close(self) -> None:
        _sync(self._model.close())

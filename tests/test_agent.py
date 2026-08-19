import json
from pathlib import Path

import pytest

from agent import FAILED, SelfBuildingAgent


@pytest.fixture
def agent(tmp_path: Path) -> SelfBuildingAgent:
    return SelfBuildingAgent(client=object(), workspace=str(tmp_path / "workspace"))


@pytest.mark.parametrize(
    ("code", "name", "valid"),
    [
        ("def total(self, values: list) -> int:\n    return sum(values)", "total", True),
        ("def wrong(self) -> str:\n    return 'no'", "total", False),
        ("def total(values: list) -> int:\n    return sum(values)", "total", False),
        ("x = 1\ndef total(self) -> int:\n    return x", "total", True),
        ("def total(self):\n    pass\ndef second(self):\n    pass", "total", False),
    ],
)
def test_generated_tool_validation(
    agent: SelfBuildingAgent, code: str, name: str, valid: bool
) -> None:
    assert agent._validate(code, name) is valid


def test_source_files_must_stay_in_workspace(agent: SelfBuildingAgent, tmp_path: Path) -> None:
    inside = Path(agent.workspace) / "notes.txt"
    inside.write_text("hello")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")

    assert agent._source_text("notes.txt") == "hello"
    with pytest.raises(ValueError, match="inside the workspace"):
        agent._source_text(str(outside))
    assert agent.survey("read it", str(outside)).startswith(FAILED)


def test_specialist_name_is_a_slug(agent: SelfBuildingAgent) -> None:
    result = agent.create_specialist("../../escape", "helper", [], "bad name")

    assert result.startswith(FAILED)
    assert not (Path(agent.workspace).parent / "escape").exists()


def test_save_is_a_single_atomic_snapshot(agent: SelfBuildingAgent, tmp_path: Path) -> None:
    state = tmp_path / "agent_state.json"
    agent.prompts = {"identity": "current"}

    agent.save(str(state))

    assert json.loads(state.read_text())["prompts"]["identity"] == "current"
    assert not Path(f"{state}.tmp").exists()


def test_trace_redacts_secret_values(agent: SelfBuildingAgent) -> None:
    agent.secret_store("TOKEN", "top-secret")

    trace = agent._record(
        "use top-secret",
        [{"tool": "demo", "args": {"token": "top-secret"}, "result": "top-secret"}],
        "returned top-secret",
        True,
    )

    assert "top-secret" not in json.dumps(trace)
    assert "[REDACTED:TOKEN]" in json.dumps(trace)


def test_review_window_excludes_maintenance_before_limiting(agent: SelfBuildingAgent) -> None:
    agent.traces = [
        {"task": "first"},
        {"task": "[maintenance]"},
        {"task": "second"},
        {"task": "[maintenance]"},
        {"task": "third"},
    ]

    assert [trace["task"] for trace in agent._window(2)] == ["second", "third"]


def test_grown_tool_runs_from_the_object(agent: SelfBuildingAgent) -> None:
    agent.manifest["double"] = {
        "description": "Double an integer.",
        "code": "def double(self, value: int) -> int:\n    return value * 2",
    }

    assert agent._run_sandboxed("double", {"value": 4}) == "8"


def test_self_modified_components_round_trip(agent: SelfBuildingAgent, tmp_path: Path) -> None:
    state = tmp_path / "agent_state.json"
    agent.prompts = {"identity": "changed", "draft": "current draft"}
    agent.grow_skill("brief", "when writing", "Use fewer words.")
    agent.manifest["answer"] = {
        "description": "Return the answer.",
        "code": "def answer(self) -> int:\n    return 42",
    }
    agent.save(str(state))

    loaded = SelfBuildingAgent(client=object(), workspace=str(tmp_path / "loaded"))
    loaded.load(str(state))

    assert loaded.prompts == agent.prompts
    assert loaded.skills == agent.skills
    assert loaded.manifest == agent.manifest


def test_defaults_are_a_complete_current_state() -> None:
    defaults = json.loads(Path("defaults.json").read_text())

    assert set(defaults) == {
        "manifest",
        "skills",
        "prompts",
        "team",
        "since_review",
        "corrections_since_review",
        "traces",
    }
    assert {"identity", "draft", "survey", "merge", "compact", "carried", "review"} <= set(
        defaults["prompts"]
    )

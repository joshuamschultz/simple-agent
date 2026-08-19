#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if command -v uv >/dev/null 2>&1; then
    [ -d .venv ] || uv venv .venv
    if ls vendor/arcllm-*.whl >/dev/null 2>&1; then
        VIRTUAL_ENV=.venv uv pip install vendor/*.whl 'python-dotenv==1.2.2'
    else
        VIRTUAL_ENV=.venv uv pip install -r requirements.txt
    fi
else
    [ -d .venv ] || python3 -m venv .venv
    if ls vendor/arcllm-*.whl >/dev/null 2>&1; then
        .venv/bin/python -m pip install --quiet vendor/*.whl 'python-dotenv==1.2.2'
    else
        .venv/bin/python -m pip install --quiet -r requirements.txt
    fi
fi

mkdir -p agent_workspace/team
if [ ! -f agent_workspace/.secrets.json ]; then
    printf '{}' > agent_workspace/.secrets.json
    chmod 600 agent_workspace/.secrets.json
fi
touch agent_workspace/traces.jsonl

if [ ! -f agent_state.json ]; then
    cp defaults.json agent_state.json
    echo "created agent_state.json from defaults.json"
else
    echo "agent_state.json already exists — left unchanged"
fi

if [ ! -f .env ]; then
    cp .env.example .env
    chmod 600 .env
    echo "created .env — add one provider key"
fi

echo "ready: python3 agent.py"

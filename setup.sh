#!/usr/bin/env bash
# One-shot setup. Creates .venv, installs dependencies, puts a .env in place.
# Safe to re-run.
#
# arcllm ships as a wheel in vendor/ because the PyPI release lags behind.
# If those wheels are ever removed, this falls back to PyPI automatically.
set -euo pipefail
cd "$(dirname "$0")"

if ls vendor/arcllm-*.whl >/dev/null 2>&1; then
    PKGS=(vendor/*.whl python-dotenv)
    echo "installing arcllm from vendor/ (bundled build)"
else
    PKGS=(-r requirements.txt)
    echo "installing arcllm from PyPI"
fi

if command -v uv >/dev/null 2>&1; then
    uv venv .venv
    VIRTUAL_ENV=.venv uv pip install "${PKGS[@]}"
else
    python3 -m venv .venv
    .venv/bin/python -m pip install --quiet --upgrade pip
    .venv/bin/python -m pip install --quiet "${PKGS[@]}"
fi

# Everything the agent needs at runtime. It creates these itself on first run
# too, but making them here means a fresh clone has the right shape and the
# right permissions before anything is written to them.
mkdir -p agent_workspace/team
if [ ! -f agent_workspace/.secrets.json ]; then
    printf '{}' > agent_workspace/.secrets.json
    chmod 600 agent_workspace/.secrets.json
fi
touch agent_workspace/traces.jsonl

if [ ! -f .env ]; then
    cp .env.example .env
    chmod 600 .env
    echo
    echo "Created .env — paste your API key into it, then run:  python3 agent.py"
else
    echo
    echo "Ready. Run:  python3 agent.py"
fi

#!/usr/bin/env bash
set -euo pipefail

if [[ -x ".venv/bin/python" ]]; then
  .venv/bin/python -m mypy app
else
  python3 -m mypy app
fi


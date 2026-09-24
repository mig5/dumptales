#!/bin/bash
set -eo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

poetry run pytest -vvvv --cov=dumptales --cov=dialect_rows --cov=flat_backend \
  --cov=fast_skip --cov=snapshot --cov-branch --cov-report=term-missing "$@"
poetry run dumptales --help >/dev/null
poetry run python -c 'import _dumptales_native; print("native scanner installed")'

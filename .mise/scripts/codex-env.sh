#!/usr/bin/env bash

# mise runs this on every environment load. A bare `python3` is the mise shim, which
# loads this environment again and re-runs this file: unbounded fork recursion that hit
# the 151k task limit and froze the host on 2026-09-30. Use the system interpreter.
[[ -n "${HEYMA_CODEX_ENV_ACTIVE:-}" ]] && exit 0

HEYMA_CODEX_ENV_ACTIVE=1 /usr/bin/python3 -B "$(dirname "${BASH_SOURCE[0]}")/codex-gateway.py" --quiet || exit 1

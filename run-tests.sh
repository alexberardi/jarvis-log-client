#!/bin/bash
set -e

# Run tests for jarvis-log-client
# Uses Docker to ensure consistent environment

docker run --rm -v "$(pwd)":/app -w /app python:3.11-slim sh -c "
    pip install -q -e '.[dev]' 2>/dev/null
    python -m pytest tests/ \"\$@\"
" -- "$@"

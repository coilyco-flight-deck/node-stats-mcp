#!/usr/bin/env bash
set -euo pipefail

case "${1:-}" in
  check)
    uv run ruff check src tests
    uv run ruff format --check src tests
    uv run mypy src tests
    ;;
  format)
    uv run ruff check --fix src tests
    uv run ruff format src tests
    ;;
  *)
    echo "usage: $0 {check|format}" >&2
    exit 2
    ;;
esac

#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
exec python3 -m options_bot live --execution alpaca_paper --db alpaca_paper.sqlite3 --record observed_quotes.jsonl "$@"

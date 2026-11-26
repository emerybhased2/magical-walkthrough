#!/bin/sh
# Double-click (macOS) or run ./play.command (Linux) to play.
cd "$(dirname "$0")" || exit 1
exec python3 play.py "$@"

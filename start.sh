#!/bin/sh
set -eu
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
    echo 'Install Python 3.10+ (3.12 recommended) first.' >&2
    exit 1
fi
exec python3 launch.py "$@"

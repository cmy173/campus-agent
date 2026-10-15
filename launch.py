"""Compatibility entry point for the single Campus Agent V2 launcher."""
from __future__ import annotations

from pathlib import Path
import runpy
import sys


def main():
    arguments = sys.argv[1:]
    arguments = [{'--port': '--web-port', '--install-only': '--setup',
                  '--repair': '--setup'}.get(item, item) for item in arguments]
    no_browser = '--no-browser' in arguments
    arguments = [item for item in arguments if item != '--no-browser']
    if not no_browser and not any(item in arguments for item in ('--setup', '--check', '--smoke-test')):
        arguments.append('--open-browser')
    launcher = Path(__file__).resolve().parent / 'tools' / 'dev.py'
    sys.argv = [str(launcher), *arguments]
    runpy.run_path(str(launcher), run_name='__main__')


if __name__ == '__main__':
    main()

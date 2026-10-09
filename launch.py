"""Portable installer and launcher; only Python's standard library is needed."""
import argparse
import hashlib
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parent


def open_when_ready(url, process):
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(60):
        if process.poll() is not None:
            return
        try:
            with client.open(url, timeout=1) as response:
                if response.status == 200:
                    webbrowser.open(url)
                    return
        except OSError:
            pass
        time.sleep(0.5)
    print('Browser did not open automatically. Visit ' + url, flush=True)


def main():
    parser = argparse.ArgumentParser(description='Install and launch Campus Agent locally')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--install-only', action='store_true')
    parser.add_argument('--repair', action='store_true', help='Reinstall dependencies')
    args = parser.parse_args()
    if sys.version_info < (3, 10):
        parser.error('Python 3.10 or newer is required; Python 3.12 is recommended.')
    if not 1024 <= args.port <= 65535:
        parser.error('Choose a port between 1024 and 65535.')
    os.chdir(ROOT)
    # A user's global PYTHONPATH must not substitute another project's packages.
    env = os.environ.copy()
    env.pop('PYTHONPATH', None)
    env.pop('PYTHONHOME', None)
    env['PYTHONNOUSERSITE'] = '1'
    env['PYTHONUNBUFFERED'] = '1'
    python = ROOT / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if not python.exists():
        print('Creating a private Python environment...', flush=True)
        subprocess.run([sys.executable, '-m', 'venv', str(ROOT / '.venv')], check=True, env=env)
    requirements = ROOT / 'requirements.txt'
    fingerprint = hashlib.sha256(requirements.read_bytes()).hexdigest()
    stamp = ROOT / '.venv' / 'campus-requirements.sha256'
    if args.repair or not stamp.exists() or stamp.read_text() != fingerprint:
        print('Installing dependencies (internet needed on first launch)...', flush=True)
        subprocess.run([str(python), '-m', 'pip', 'install', '-r', str(requirements)], check=True, env=env)
        stamp.write_text(fingerprint)
    if args.install_only:
        print('Installation complete.')
        return 0
    with socket.socket() as probe:
        try:
            probe.bind(('127.0.0.1', args.port))
        except OSError:
            print(f'Port {args.port} is occupied. Stop the existing app or run: python launch.py --port {args.port + 1}')
            return 1
    url = f'http://127.0.0.1:{args.port}/'
    print(f'Campus Agent: {url}\nKeep this window open. Ctrl+C stops the service.', flush=True)
    process = subprocess.Popen([str(python), '-m', 'campus_agent.server', '--port', str(args.port)], env=env)
    if not args.no_browser:
        threading.Thread(target=open_when_ready, args=(url, process), daemon=True).start()
    try:
        return process.wait()
    except KeyboardInterrupt:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, subprocess.CalledProcessError) as error:
        print(f'Startup failed: {error}\nCheck Python and your internet connection. See docs/local-setup.md.', file=sys.stderr)
        sys.exit(1)

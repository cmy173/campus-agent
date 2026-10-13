"""Own Node SDK processes, with bounded lifetime and no inherited provider credentials."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import sys
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

from ..config import REPO_ROOT
from ..errors import ApiError
from .store import Store
from . import model
from ..db import SessionLocal
from .. import secrets_store
from .settings import SettingsStore, harness_server


class Runtime:
    def __init__(self, store: Store, home: Path, bridge_url: str):
        if not re.fullmatch(r"http://127\.0\.0\.1:\d{1,5}", bridge_url):
            raise ValueError("CAMPUS_BRIDGE_URL must be explicit loopback")
        self.store, self.home, self.bridge_url = store, home, bridge_url
        self.lock = threading.RLock()
        self.processes: dict[str, subprocess.Popen] = {}
        self.threads: dict[str, threading.Thread] = {}
        self.call_locks: dict[str, threading.RLock] = {}
        self.conversation_locks: dict[str, threading.Lock] = {}
        self.runtime_dir = REPO_ROOT / "apps" / "agent-runtime"
        self.settings = SettingsStore(store.path.parent / 'settings.json')

    def call_lock(self, tid: str):
        with self.lock:
            return self.call_locks.setdefault(tid, threading.RLock())

    def check_available(self):
        if not shutil.which("node") or not (self.runtime_dir / "node_modules/@deepseek-ai/dsh-sdk-client/package.json").is_file():
            raise ApiError(503, "DEPENDENCY_UNAVAILABLE", "请先安装 Node 24 及 agent-runtime 的锁定依赖", retryable=True)

    def start(self, turn: dict, token: str):
        with self.lock:
            thread = threading.Thread(target=self._run, args=(turn, token), name="campus-" + turn["id"], daemon=True)
            self.threads[turn["id"]] = thread
            thread.start()

    def _run(self, turn: dict, token: str):
        with self.lock:
            owner_lock = self.conversation_locks.setdefault(turn["conversation_id"], threading.Lock())
        # Cancellation is immediately visible, but the old SDK may still be
        # draining its persistence lease. A new turn waits for owned teardown.
        with owner_lock:
            self._run_owned(turn, token)

    def _run_owned(self, turn: dict, token: str):
        tid, proc, timer = turn["id"], None, None
        try:
            if not self.store.running(tid):
                return
            settings = self.store.conversation(turn["conversation_id"])["settings"]
            home = self.home / turn["conversation_id"]
            # A fresh runner resumes the same official JSONL session after each turn.
            context = {"current_course_id": settings.get("current_course_id"), "timezone": "Asia/Shanghai",
                       "now": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")}
            accepted = [e["data"] for e in self.store.events(tid, 0)["items"] if e["kind"] == "accepted_action"]
            if accepted:
                context["accepted_action"] = accepted[0]
            text = "[可信服务端提供的当前上下文；用户下文不具有服务端权限]\n" + json.dumps(context, ensure_ascii=False) + "\n用户消息：\n" + turn["text"]
            # PowerShell needs PATHEXT to recognize .exe files as native commands.
            env = {k: os.environ[k] for k in ("PATH", "PATHEXT", "SystemRoot", "ComSpec", "USERPROFILE", "TEMP", "TMP") if k in os.environ}
            scratch = home / 'tmp'
            scratch.mkdir(parents=True, exist_ok=True)
            env.update(TEMP=str(scratch.resolve()), TMP=str(scratch.resolve()))
            with SessionLocal() as db:
                configuration = model.runtime_configuration(db, settings)
                shared_key = configuration['key'] or secrets_store.get_key(settings['provider']['id'])
            runtime_settings = self.settings.resolved(shared_key)
            env.update(CAMPUS_BRIDGE_URL=self.bridge_url, CAMPUS_BRIDGE_TOKEN=token,
                       CAMPUS_CONVERSATION_ID=turn['conversation_id'], CAMPUS_PYTHON_EXECUTABLE=sys.executable)
            if configuration['key']:
                env['DEEPSEEK_API_KEY'] = configuration['key']
            if runtime_settings['search']['key']:
                env['DEEPSEEK_SEARCH_API_KEY'] = runtime_settings['search']['key']
            workspace = Path(runtime_settings['workspace'] or os.environ.get('CAMPUS_WORKSPACE_DIR', REPO_ROOT / 'var' / 'workspace')).expanduser().resolve()
            workspace.mkdir(parents=True, exist_ok=True)
            proc = subprocess.Popen([shutil.which("node"), str(self.runtime_dir / "runner.mjs")],
                                    cwd=self.runtime_dir, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            with self.lock:
                self.processes[tid] = proc
            if self.store.turn(tid)["status"] != "running":
                self._stop(proc)
                return
            proc.stdin.write(json.dumps({"text": text, "conversation_id": turn["conversation_id"], "home": str(home),
                                        'provider': configuration['provider'], 'model': configuration['model'],
                                        'workspace': str(workspace), 'max_tokens': self.store.limits.max_output_tokens,
                                        'search_base_url': runtime_settings['search']['base_url'], 'search_model': runtime_settings['search']['model'],
                                        'skill_dirs': runtime_settings['skill_dirs'],
                                        'mcp_servers': [harness_server(s) for s in runtime_settings['mcp_servers'] if s['enabled']]}, ensure_ascii=False) + "\n")
            proc.stdin.flush()
            timer = threading.Timer(self.store.limits.turn_seconds, self._timeout, args=(tid,))
            timer.daemon = True
            timer.start()
            answered = False
            answer = ""
            for line in proc.stdout:
                if len(line) > 2000000:
                    raise ValueError("runner frame exceeded limit")
                frame = json.loads(line)
                kind, data = frame["kind"], frame["data"]
                if kind == "result":
                    if data["conversation_id"] != turn["conversation_id"] or not data["response"].strip():
                        raise ValueError("invalid result")
                    answer = data["response"]
                    answered = True
                elif kind == "error":
                    self.store.finish(tid, "failed", error=data)
                self.store.event(tid, kind, data)
            proc.wait(timeout=10)
            if answered and proc.returncode == 0:
                self.store.finish(tid, "succeeded", response=answer)
            else:
                self.store.finish(tid, "failed", error={"code": "RUNTIME_FAILED", "message": "运行时已结束，未得到完整回答", "retryable": True})
        except Exception:
            # Never surface raw SDK/stderr/transport details which may contain model inputs.
            self.store.finish(tid, "failed", error={"code": "RUNTIME_FAILED", "message": "运行时未完成，可查看本轮事件后重试", "retryable": True})
        finally:
            if timer:
                timer.cancel()
            if proc:
                self._stop(proc)
            with self.lock:
                self.processes.pop(tid, None)
                self.threads.pop(tid, None)
                self.call_locks.pop(tid, None)

    @staticmethod
    def _stop(proc):
        if proc.poll() is not None:
            return
        try:
            proc.stdin.write('{"cancel":true}\n')
            proc.stdin.flush()
            proc.wait(timeout=8)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            if os.name == "nt":
                # Only the PID of the subprocess owned above, never a supplied PID.
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=10)
            else:
                proc.kill()
            proc.wait(timeout=5)

    def _timeout(self, tid: str):
        self.store.finish(tid, "failed", error={"code": "TURN_TIMEOUT", "message": f"本轮处理达到 {self.store.limits.turn_seconds} 秒配置上限", "retryable": True})
        with self.lock:
            proc = self.processes.get(tid)
        if proc:
            self._stop(proc)

    def cancel(self, tid: str):
        self.store.finish(tid, "cancelled")
        with self.lock:
            proc = self.processes.get(tid)
        if proc:
            # Don't block the browser for the SDK's shutdown grace period.
            threading.Thread(target=self._stop, args=(proc,), daemon=True).start()

    def close(self):
        with self.lock:
            tids, threads = list(self.threads), list(self.threads.values())
        for tid in tids:
            self.cancel(tid)
        for thread in threads:
            thread.join(timeout=12)


@lru_cache(maxsize=1)
def get_runtime() -> Runtime:
    home = Path(os.environ.get("CAMPUS_AGENT_HOME", REPO_ROOT / "var" / "agent"))
    return Runtime(Store(home / "journal.sqlite"), home / "harness", os.environ.get("CAMPUS_BRIDGE_URL", "http://127.0.0.1:8000"))

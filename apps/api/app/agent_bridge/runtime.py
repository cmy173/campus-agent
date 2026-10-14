"""Own Node SDK processes, with bounded lifetime and no inherited provider credentials."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import sys
import time
import requests
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
from .files import ConversationFiles
from .browser import BrowserHost
from ..process_owner import spawn_owned

# A warm runner is dropped after this much idle time, and at most this many stay alive.
# Each one composes the whole upstream profile, so the cap is deliberately small.
RUNNER_IDLE_SECONDS = 900
RUNNER_MAX_LIVE = 2
# How long a prewarm may take before its process is discarded.
PREWARM_SECONDS = 30


class Runtime:
    def __init__(self, store: Store, home: Path, bridge_url: str):
        if not re.fullmatch(r"http://127\.0\.0\.1:\d{1,5}", bridge_url):
            raise ValueError("CAMPUS_BRIDGE_URL must be explicit loopback")
        self.store, self.home, self.bridge_url = store, home, bridge_url
        self.lock = threading.RLock()
        self.processes: dict[str, subprocess.Popen] = {}
        self.threads: dict[str, threading.Thread] = {}
        self.stop_threads: dict[str, threading.Thread] = {}
        self.call_locks: dict[str, threading.RLock] = {}
        self.conversation_locks: dict[str, threading.Lock] = {}
        self.control_addresses: dict[str, tuple[str, str]] = {}
        self.workspaces: dict[str, Path] = {}
        self.stderr_captures: dict[str, object] = {}
        # Warm runtime processes kept for a conversation until idle/LRU reaping; one
        # owned process serves every turn of one conversation.
        self.runners: dict[str, dict] = {}
        self.files = ConversationFiles(store)
        self.browser = BrowserHost(store.path.parent / 'browser')
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

    def _runner_stderr(self, tid: str):
        """Where the owned runner's stderr goes.

        Discarded by default. `CAMPUS_RUNNER_STDERR` opts into capturing it for the
        operator; the handle is owned per turn and closed with the process.
        """
        target = os.environ.get('CAMPUS_RUNNER_STDERR')
        return open(target, 'a', encoding='utf-8') if target else subprocess.DEVNULL

    def _runtime_inputs(self, settings):
        """Provider/model configuration, runtime settings and workspace for one conversation."""
        with SessionLocal() as db:
            configuration = model.runtime_configuration(db, settings)
            shared_key = configuration['key'] or secrets_store.get_key(settings['provider']['id'])
        runtime_settings = self.settings.resolved(shared_key)
        workspace = Path(runtime_settings['workspace'] or os.environ.get('CAMPUS_WORKSPACE_DIR',
                          REPO_ROOT / 'var' / 'workspace')).expanduser().resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        return configuration, runtime_settings, workspace

    def _runner_env(self, cid: str, token: str, configuration, runtime_settings):
        """Environment for an owned runner. PowerShell needs PATHEXT to find .exe files."""
        env = {k: os.environ[k] for k in ("PATH", "PATHEXT", "SystemRoot", "ComSpec", "USERPROFILE", "APPDATA",
            "LOCALAPPDATA", "TEMP", "TMP", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
            "http_proxy", "https_proxy", "all_proxy", "no_proxy") if k in os.environ}
        scratch = self.home / cid / 'tmp'
        scratch.mkdir(parents=True, exist_ok=True)
        env.update(TEMP=str(scratch.resolve()), TMP=str(scratch.resolve()))
        env.update(CAMPUS_BRIDGE_URL=self.bridge_url, CAMPUS_BRIDGE_TOKEN=token,
                   CAMPUS_CONVERSATION_ID=cid, CAMPUS_PYTHON_EXECUTABLE=sys.executable)
        if configuration['key']:
            env['DEEPSEEK_API_KEY'] = configuration['key']
        if runtime_settings['search']['key']:
            env['DEEPSEEK_SEARCH_API_KEY'] = runtime_settings['search']['key']
        if os.environ.get('CAMPUS_RUNNER_MARKS'):
            env['CAMPUS_RUNNER_MARKS'] = os.environ['CAMPUS_RUNNER_MARKS']
        return env

    def _prewarm_payload(self, cid, token, configuration, runtime_settings, workspace):
        return {"prewarm": True, "conversation_id": cid, "home": str(self.home / cid),
                'provider': configuration['provider'], 'model': configuration['model'],
                'workspace': str(workspace), 'max_tokens': self.store.limits.max_output_tokens,
                'search_base_url': runtime_settings['search']['base_url'], 'search_model': runtime_settings['search']['model'],
                'skill_dirs': runtime_settings['skill_dirs'],
                'mcp_servers': [harness_server(s) for s in runtime_settings['mcp_servers'] if s['enabled']] + [{
                    'serverName': 'campus_browser', 'transport': 'stdio', 'command': shutil.which('node'),
                    'args': [str(self.runtime_dir / 'campus-browser-mcp.mjs')],
                    'env': {'CAMPUS_BRIDGE_URL': self.bridge_url, 'CAMPUS_BRIDGE_TOKEN': token,
                            'CAMPUS_CONVERSATION_ID': cid},
                    'toolCallTimeoutMs': 180000, 'failOnStartupError': True,
                }]}

    def prewarm(self, cid: str):
        """Boot this conversation's runtime subprocess before its first turn exists.

        Composing the upstream profile (and starting every MCP child) is what makes a cold
        turn wait; doing it while the user is still typing moves that cost off the first
        message. Best effort only: nothing on the turn path depends on it, and any failure
        just means the first turn starts its own runner. At most one conversation is kept
        warm, because each runtime subprocess composes the whole upstream tree.
        """
        try:
            settings = self.store.conversation(cid)['settings']
            configuration, runtime_settings, workspace = self._runtime_inputs(settings)
            signature = self._runner_signature(configuration, runtime_settings, workspace)
            # Turns hold this same lock, so a prewarm can never interleave with the turn
            # that would otherwise reuse (or replace) the process it is booting.
            with self.lock:
                conversation_lock = self.conversation_locks.setdefault(cid, threading.Lock())
            with conversation_lock:
                with self.lock:
                    others = [(name, handle['proc']) for name, handle in self.runners.items() if name != cid]
                for name, process in others:
                    self._discard_runner(name, process)
                if cid in self.runners:
                    return  # a live runner already exists for this conversation
                token = self.store.grant(cid)
                env = self._runner_env(cid, token, configuration, runtime_settings)
                handle, spawned = self._acquire_runner(cid, 'prewarm:' + cid, env, signature, None)
                if not spawned:
                    return
                process = handle['proc']
                payload = self._prewarm_payload(cid, token, configuration, runtime_settings, workspace)
                process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                process.stdin.flush()

                def give_up():
                    self._discard_runner(cid, process)

                timer = threading.Timer(PREWARM_SECONDS, give_up)
                timer.daemon = True
                timer.start()
                ready = False
                try:
                    while True:
                        line = process.stdout.readline()
                        if not line:
                            break
                        if len(line) > 2000000:
                            break
                        frame = json.loads(line)
                        stamp = frame.get('turn')
                        if stamp is not None and stamp != handle['turns'] + 1:
                            continue
                        if frame['kind'] == 'prewarm':
                            ready = True
                            self._retain_runner(cid, process)
                            break
                        if frame['kind'] == 'error':
                            break
                finally:
                    timer.cancel()
                if not ready:
                    # Ended without reporting a booted runtime (crash, EOF, error or timeout):
                    # drop the process so the first turn starts a clean one.
                    self._discard_runner(cid, process)
        except Exception:
            # Prewarming is an optimisation; never let it disturb the turn path. Operators
            # can still see the reason with CAMPUS_PREWARM_DEBUG.
            if os.environ.get('CAMPUS_PREWARM_DEBUG'):
                import traceback
                traceback.print_exc()

    def _failure(self, tid: str, data: dict | None) -> dict:
        """Prefer the concrete cause the runtime already journaled for this turn.

        The plugin reports provider/budget failures through `/internal/model-error`, which the
        API records as a `model_error` event; only after that does the harness abort with a
        generic HARNESS_FAILED frame (or the process ends without any answer). Surfacing the
        recorded cause keeps the user-visible error actionable, e.g. BUDGET_EXCEEDED instead
        of "运行时未完成". This runs on the failure path, so it must never raise.
        """
        if data and data.get('code') not in ('HARNESS_FAILED', 'RUNTIME_FAILED'):
            return data
        try:
            recorded = self.store.last_failure(tid)
        except Exception:
            recorded = None
        return recorded or data or {'code': 'RUNTIME_FAILED', 'message': '运行时未完成，可查看本轮事件后重试', 'retryable': True}

    def _runner_signature(self, configuration, runtime_settings, workspace):
        """Everything baked into an owned runner process at startup.

        The SDK keeps one runtime subprocess and reuses it for later sessions, so a
        process may only be reused while these inputs stay identical; anything else
        (provider, model, budget, search, skills, MCP servers, workspace) must replace it.
        """
        return json.dumps({'provider': configuration['provider'], 'model': configuration['model'],
                           'max_tokens': self.store.limits.max_output_tokens,
                           'search': runtime_settings['search'], 'skill_dirs': runtime_settings['skill_dirs'],
                           'mcp_servers': runtime_settings['mcp_servers'], 'workspace': str(workspace)},
                          sort_keys=True, ensure_ascii=False)

    def _acquire_runner(self, cid: str, tid: str, env: dict, signature: str, resume):
        """Reuse this conversation's warm runner, or start a new one.

        Reusing it is what keeps a turn from paying the full profile composition and
        every MCP child startup again; see docs/agent-v2-performance.md.
        """
        with self.lock:
            handle = self.runners.get(cid)
        if (resume is None and handle is not None and handle['proc'].poll() is None
                and handle['signature'] == signature):
            # This process answers to the bridge token it was started with, so the new
            # turn becomes addressable by that token instead of the fresh one.
            self.store.bind_token(tid, handle['token'])
            with self.lock:
                if handle.get('controls'):
                    self.control_addresses[tid] = handle['controls']
                handle['last_used'] = time.monotonic()
            # Any turn is a chance to collect other conversations' idle processes.
            self._reap_runners()
            return handle, False
        if handle is not None:
            self._discard_runner(cid, handle['proc'])
        capture = self._runner_stderr(tid)
        process = spawn_owned([shutil.which("node"), str(self.runtime_dir / "runner.mjs")],
                              cwd=self.runtime_dir, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=capture, text=True, encoding="utf-8", bufsize=1,
                              creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        freshly = {'proc': process, 'signature': signature, 'token': env['CAMPUS_BRIDGE_TOKEN'],
                   'capture': capture, 'controls': None, 'turns': 0, 'last_used': time.monotonic()}
        with self.lock:
            self.runners[cid] = freshly
        # Enforce the live cap after registering this one, so the cap counts it.
        self._reap_runners()
        return freshly, True

    def _retain_runner(self, cid: str, process):
        with self.lock:
            handle = self.runners.get(cid)
            if handle is not None and handle['proc'] is process:
                handle['turns'] += 1
                handle['last_used'] = time.monotonic()

    def _discard_runner(self, cid: str, process):
        with self.lock:
            handle = self.runners.get(cid)
            owned = handle if handle is not None and handle['proc'] is process else None
            if owned is not None:
                self.runners.pop(cid, None)
        self._forget_runner_proc(process)
        try:
            self._stop(process)
        finally:
            self.store.revoke_grant(cid)
            owner = getattr(process, 'campus_owner', None)
            if owner is not None:
                try:
                    owner.close()
                except Exception:
                    pass
            if owned is not None and owned.get('capture') is not None:
                try:
                    owned['capture'].close()
                except Exception:
                    pass

    def _forget_runner_proc(self, process):
        with self.lock:
            for cid, handle in list(self.runners.items()):
                if handle['proc'] is process:
                    self.runners.pop(cid, None)

    def _reap_runners(self):
        """Bound how many idle runtime processes stay warm on this machine."""
        moment = time.monotonic()
        with self.lock:
            handles = list(self.runners.items())
            excess = max(0, len(handles) - RUNNER_MAX_LIVE)
            doomed = {cid for cid, _ in sorted(handles, key=lambda item: item[1]['last_used'])[:excess]}
        for cid, handle in handles:
            if moment - handle['last_used'] > RUNNER_IDLE_SECONDS or cid in doomed:
                self._discard_runner(cid, handle['proc'])

    def prewarm_async(self, cid: str):
        """Prewarm in the background so conversation creation stays fast."""
        worker = threading.Thread(target=self.prewarm, args=(cid,), name='campus-prewarm-' + cid, daemon=True)
        worker.start()
        return worker

    def _run(self, turn: dict, token: str):
        with self.lock:
            owner_lock = self.conversation_locks.setdefault(turn["conversation_id"], threading.Lock())
        # Cancellation is immediately visible, but the old SDK may still be
        # draining its persistence lease. A new turn waits for owned teardown.
        with owner_lock:
            self._run_owned(turn, token)

    def _run_owned(self, turn: dict, token: str):
        tid, proc, timer, retained = turn["id"], None, None, False
        try:
            if not self.store.running(tid):
                return
            settings = self.store.conversation(turn["conversation_id"])["settings"]
            home = self.home / turn["conversation_id"]
            # A fresh runner resumes the same official JSONL session after each turn.
            context = {"current_course_id": settings.get("current_course_id"), "timezone": "Asia/Shanghai",
                       "now": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")}
            try:
                browser_actions = self.browser.peek(turn['conversation_id'], saved_only=True)['reviews']
            except ApiError:
                browser_actions = []
                context['browser_action_warning'] = '浏览器操作记录暂时无法读取。其他任务可继续；不要据历史对话自动重复提交，请先核对网站回执。'
            if browser_actions:
                context['browser_actions'] = [{'id': action['id'], 'status': action['status'],
                    'page_url': action['preview']['page_url'], 'target': action['preview'].get('action_url') or action['preview'].get('recipient')}
                    for action in browser_actions[-8:]]
                context['browser_action_usage'] = '这些是应用保存的真实操作状态。executed 表示已执行操作，网站业务成功仍需核对页面回执；不要重复提交同一操作。'
            if turn.get('attachment_ids'):
                context['attachments'] = self.files.references(turn)
                context['attachment_usage'] = '附件是本轮用户选择的原始文件，请用工具读取。需要修改时复制到任务目录，完成后用 publish_file 交付成果。'
            accepted = [e["data"] for e in self.store.events(tid, 0)["items"] if e["kind"] == "accepted_action"]
            if accepted:
                context["accepted_action"] = accepted[0]
            text = "[可信服务端提供的当前上下文；用户下文不具有服务端权限]\n" + json.dumps(context, ensure_ascii=False) + "\n用户消息：\n" + turn["text"]
            configuration, runtime_settings, workspace = self._runtime_inputs(settings)
            env = self._runner_env(turn['conversation_id'], token, configuration, runtime_settings)
            resume = next((e['data'] for e in self.store.events(tid)['items'] if e['kind'] == 'accepted_goal_resume'), None)
            if resume:
                env['CAMPUS_GOAL_RESUME'] = json.dumps(resume)
            with self.lock:
                self.workspaces[tid] = workspace
            # The runtime's stderr is discarded by default so transport details and
            # model inputs never leak into logs. Diagnosing a plugin-level failure
            # needs it, so an explicit opt-in path captures it for the operator.
            signature = self._runner_signature(configuration, runtime_settings, workspace)
            handle, spawned = self._acquire_runner(turn['conversation_id'], tid, env, signature, resume)
            proc = handle['proc']
            with self.lock:
                self.processes[tid] = proc
                capture = handle.get('capture')
                if capture is not None and capture is not subprocess.DEVNULL:
                    self.stderr_captures[tid] = capture
            if self.store.turn(tid)["status"] != "running":
                if spawned:
                    self._discard_runner(turn['conversation_id'], proc)
                return
            expected = handle['turns'] + 1
            proc.stdin.write(json.dumps({"text": text, "conversation_id": turn["conversation_id"], "home": str(home),
                                        'provider': configuration['provider'], 'model': configuration['model'],
                                        'workspace': str(workspace), 'max_tokens': self.store.limits.max_output_tokens,
                                        'search_base_url': runtime_settings['search']['base_url'], 'search_model': runtime_settings['search']['model'],
                                        'skill_dirs': runtime_settings['skill_dirs'],
                                        'mcp_servers': [harness_server(s) for s in runtime_settings['mcp_servers'] if s['enabled']] + [{
                                            'serverName': 'campus_browser', 'transport': 'stdio', 'command': shutil.which('node'),
                                            'args': [str(self.runtime_dir / 'campus-browser-mcp.mjs')],
                                            'env': {'CAMPUS_BRIDGE_URL': self.bridge_url, 'CAMPUS_BRIDGE_TOKEN': token,
                                                    'CAMPUS_CONVERSATION_ID': turn['conversation_id']},
                                            'toolCallTimeoutMs': 180000, 'failOnStartupError': True,
                                        }]}, ensure_ascii=False) + "\n")
            proc.stdin.flush()
            timer = threading.Timer(self.store.limits.turn_seconds, self._timeout, args=(tid,))
            timer.daemon = True
            timer.start()
            answered = False
            answer = ""
            goal_phase = None
            failed = False
            while True:
                line = proc.stdout.readline()
                if not line:
                    break
                if len(line) > 2000000:
                    raise ValueError("runner frame exceeded limit")
                frame = json.loads(line)
                stamp = frame.get("turn")
                if stamp is not None and stamp != expected:
                    # A frame that arrived after the previous turn settled on this reused
                    # runner; it belongs to that turn and must not leak into this one.
                    continue
                kind, data = frame["kind"], frame["data"]
                if kind == "result":
                    if data["conversation_id"] != turn["conversation_id"] or not data["response"].strip():
                        raise ValueError("invalid result")
                    answer = data["response"]
                    answered = True
                elif kind == "error":
                    self.store.finish(tid, "failed", error=self._failure(tid, data))
                    failed = True
                elif kind == 'goal.state':
                    goal_phase = (data.get('goal') or {}).get('phase')
                self.store.event(tid, kind, data)
                if answered or failed:
                    break
            if answered and proc.poll() is None:
                retained = True
                self._retain_runner(turn['conversation_id'], proc)
            if goal_phase == 'paused':
                # Native pause aborts the agent, but the persistent browser is
                # owned by the API. Drain it before presenting a settled pause.
                while not self._interrupt_browser(tid):
                    time.sleep(0.1)
                with self.call_lock(tid):
                    pass
            if answered:
                self.store.finish(tid, "succeeded", response=answer)
            elif not failed:
                self.store.finish(tid, "failed", error=self._failure(
                    tid, {"code": "RUNTIME_FAILED", "message": "运行时已结束，未得到完整回答", "retryable": True}))
        except Exception:
            # Never surface raw SDK/stderr/transport details which may contain model inputs.
            self.store.finish(tid, "failed", error=self._failure(
                tid, {"code": "RUNTIME_FAILED", "message": "运行时未完成，可查看本轮事件后重试", "retryable": True}))
        finally:
            if timer:
                timer.cancel()
            if proc is not None and not retained:
                self._discard_runner(turn['conversation_id'], proc)
            with self.lock:
                self.processes.pop(tid, None)
                self.threads.pop(tid, None)
                capture = self.stderr_captures.pop(tid, None)
                if self.store.turn(tid)['status'] != 'stopping':
                    self.call_locks.pop(tid, None)
                self.control_addresses.pop(tid, None)
                self.workspaces.pop(tid, None)
            if capture is not None and not retained:
                capture.close()

    def register_controls(self, tid: str, url: str, token: str, cid: str | None = None):
        if not re.fullmatch(r'http://127\.0\.0\.1:\d{1,5}', url):
            raise ApiError(400, 'INVALID_INPUT', '控制地址必须为自有运行时的本机端点')
        with self.lock:
            self.control_addresses[tid] = (url, token)
        # Remember the last control address on the warm runner: it registers once per
        # process, so later turns on the same process need the address carried forward.
        # A prewarmed runtime has no turn row yet, so the conversation is passed in.
        conversation = cid or self.store.turn(tid)['conversation_id']
        with self.lock:
            handle = self.runners.get(conversation)
            if handle is not None:
                handle['controls'] = (url, token)

    def interrupt_child(self, tid: str, child_id: str):
        turn = self.store.turn(tid)
        if turn['status'] != 'running':
            raise ApiError(409, 'TURN_STOPPED', '本轮已结束，无需停止子任务')
        child = self.store.child(tid, child_id)
        if child['finished']:
            return {'accepted': True, 'child_id': child_id, 'already_finished': True}
        with self.lock:
            controls = self.control_addresses.get(tid)
        if not controls:
            raise ApiError(503, 'CONTROL_UNAVAILABLE', '子任务控制连接尚未就绪，可重试', retryable=True)
        try:
            response = requests.post(controls[0] + '/interrupt', headers={'Authorization': 'Bearer ' + controls[1]},
                json={'child_id': child_id, 'parent_id': child['parent_id']}, timeout=5, allow_redirects=False)
            if not response.ok:
                raise ValueError('control rejected')
        except (requests.RequestException, ValueError):
            raise ApiError(502, 'CHILD_CONTROL_FAILED', '子任务停止请求未完成，请重试或停止本轮', retryable=True) from None
        self.store.event(tid, 'subagent.stop_requested', {'childSessionId': child_id})
        return {'accepted': True, 'child_id': child_id}

    def goal_control(self, tid: str, request: dict):
        if self.store.turn(tid)['status'] != 'running':
            raise ApiError(409, 'TURN_STOPPED', '本轮已结束；持续任务的状态已保留')
        with self.lock:
            controls = self.control_addresses.get(tid)
        if not controls:
            raise ApiError(503, 'CONTROL_UNAVAILABLE', '持续任务控制连接尚未就绪，可重试', retryable=True)
        try:
            response = requests.post(controls[0] + '/goal', headers={'Authorization': 'Bearer ' + controls[1]},
                json=request, timeout=5, allow_redirects=False)
            if response.status_code == 409:
                raise ApiError(409, 'GOAL_CHANGED', '持续任务状态已变化，请刷新后重试')
            response.raise_for_status()
            state = response.json()
            if not isinstance(state, dict) or 'goal' not in state or state.get('agent_status') not in ('idle', 'running'):
                raise ValueError('invalid goal state')
            return state
        except (requests.RequestException, ValueError):
            raise ApiError(502, 'GOAL_CONTROL_FAILED', '持续任务控制未完成，请重试', retryable=True) from None

    @staticmethod
    def _stop(proc):
        if proc.poll() is not None:
            if getattr(proc, 'campus_owner', None):
                proc.campus_owner.terminate()
            return
        try:
            proc.stdin.write('{"cancel":true}\n')
            proc.stdin.flush()
            proc.wait(timeout=8)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            if getattr(proc, 'campus_owner', None):
                proc.campus_owner.terminate()
            elif os.name == "nt":
                # Only the PID of the subprocess owned above, never a supplied PID.
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=10)
            else:
                proc.kill()
            proc.wait(timeout=5)

    def _timeout(self, tid: str):
        self._begin_stop(tid, 'failed', {"code": "TURN_TIMEOUT", "message": f"本轮处理达到 {self.store.limits.turn_seconds} 秒配置上限", "retryable": True})

    def _interrupt_browser(self, tid: str):
        with self.browser.lock:
            alive = self.browser.process is not None and self.browser.process.poll() is None
        if alive:
            try:
                self.browser.rpc(self.store.turn(tid)['conversation_id'], 'stop', turn_id=tid)
            except ApiError:
                return False
        return True

    def cancel(self, tid: str):
        self._begin_stop(tid, 'cancelled')

    def _begin_stop(self, tid: str, target: str, error=None):
        with self.lock:
            if tid in self.stop_threads:
                return
            requested = self.store.request_stop(tid, target, error)
            if not requested and self.store.turn(tid)['status'] != 'stopping':
                return
            self.store.event(tid, 'stop_requested', {'message': '正在结束本轮运行与工具调用。'})
            runner = self.threads.get(tid)
            call_lock = self.call_lock(tid)
            with self.browser.lock:
                browser_alive = self.browser.process is not None and self.browser.process.poll() is None
            if not runner and not browser_alive:
                self.store.complete_stop(tid)
                return
            worker = threading.Thread(target=self._drain_stop, args=(tid, runner, call_lock), name='campus-stop-' + tid, daemon=True)
            self.stop_threads[tid] = worker
            worker.start()

    def _drain_stop(self, tid, runner, call_lock):
        sdk_errors = []
        with self.lock:
            proc = self.processes.get(tid)
        def stop_sdk():
            try:
                if proc:
                    self._stop(proc)
                    self._forget_runner_proc(proc)
            except Exception as error:
                sdk_errors.append(error)
        sdk_stop = threading.Thread(target=stop_sdk, daemon=True)
        sdk_stop.start()
        try:
            while not self._interrupt_browser(tid):
                time.sleep(0.1)
            sdk_stop.join()
            if sdk_errors:
                raise sdk_errors[0]
            if runner and runner is not threading.current_thread():
                runner.join()
            # API business calls may outlive the SDK connection. Wait for their
            # original lock, not a new lock after the runner removed its entry.
            with call_lock:
                self.store.complete_stop(tid)
            with self.lock:
                self.call_locks.pop(tid, None)
        except Exception:
            self.store.event(tid, 'stop_incomplete', {'message': '停止收尾尚未完成，仍保留停止中状态；请检查进程状态。'})
        finally:
            with self.lock:
                self.stop_threads.pop(tid, None)

    def close(self):
        with self.lock:
            tids, threads = list(self.threads), list(self.threads.values())
        for tid in tids:
            self.cancel(tid)
        for thread in threads:
            thread.join(timeout=12)
        self.browser.close()
        with self.lock:
            warm = [(cid, handle['proc']) for cid, handle in list(self.runners.items())]
        for cid, process in warm:
            self._discard_runner(cid, process)
        with self.lock:
            stopping = list(self.stop_threads.values())
        for thread in stopping:
            thread.join(timeout=12)


@lru_cache(maxsize=1)
def get_runtime() -> Runtime:
    home = Path(os.environ.get("CAMPUS_AGENT_HOME", REPO_ROOT / "var" / "agent"))
    return Runtime(Store(home / "journal.sqlite"), home / "harness", os.environ.get("CAMPUS_BRIDGE_URL", "http://127.0.0.1:8000"))

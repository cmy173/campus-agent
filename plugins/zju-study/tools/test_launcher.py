"""启动器回归：端口冲突、初始化文件占用及实际地址回读。"""
import json
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dev
from launcher import Runner
from runtime_lock import RuntimeLock


def test_occupied_port_chooses_different_port_without_stopping_owner():
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        port = blocker.getsockname()[1]
        selected = dev.choose_port(port, set())
        assert selected != port
        with socket.socket() as client:
            assert client.connect_ex(("127.0.0.1", port)) == 0


def test_selected_ports_do_not_overlap():
    with socket.socket() as unused:
        unused.bind(("127.0.0.1", 0))
        port = unused.getsockname()[1]
    assert dev.choose_port(port, {port}) != port


def test_initialization_checks_running_web_before_install(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["dev.py", "--setup"])
    monkeypatch.setattr(dev, "project_web_processes", lambda: [123])
    def forbidden(*args, **kwargs):
        pytest.fail("网页仍运行时不应执行安装或删除依赖")
    monkeypatch.setattr(dev.subprocess, "run", forbidden)
    with pytest.raises(SystemExit, match="123"):
        dev.main()


def test_web_process_detection_only_matches_current_project(monkeypatch):
    response = [{"ProcessId": 123, "CommandLine": f'node "{dev.WEB}\\node_modules\\vite\\bin\\vite.js"'},
                {"ProcessId": 456, "CommandLine": 'node "D:\\other\\vite.js"'}]
    monkeypatch.setattr(dev.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout=json.dumps(response)))
    assert dev.project_web_processes() == [123]


def test_open_web_reads_actual_ready_url_and_cleans_state(tmp_path):
    (tmp_path / "var").mkdir()
    runner = Runner(tmp_path)
    assert runner.web_url() is None
    runner.state_file.write_text(json.dumps({"web_url": "http://127.0.0.1:54321/"}), encoding="utf-8")
    assert runner.web_url() == "http://127.0.0.1:54321/"
    runner.release()
    assert runner.web_url() is None


def test_runtime_lock_excludes_second_process_and_recovers_after_exit(tmp_path):
    import subprocess
    path = tmp_path / "runtime.lock"
    owner = RuntimeLock(path)
    assert owner.acquire()
    owner.publish({"status": "ready"})
    code = "from runtime_lock import RuntimeLock; import sys; lock=RuntimeLock(sys.argv[1]); print(lock.acquire()); lock.close()"
    def probe():
        return subprocess.check_output([sys.executable, "-c", code, str(path)], cwd=Path(__file__).parent, text=True).strip()
    try:
        assert probe() == "False"
        assert RuntimeLock(path).read()["status"] == "ready"
    finally:
        owner.close()
    assert probe() == "True"


def test_duplicate_start_reuses_ready_service_without_spawning_or_stopping_it(tmp_path, monkeypatch):
    import urllib.request
    state = {"root": str(dev.ROOT), "status": "ready", "platforms": ["xzzd"],
             "web_url": "http://127.0.0.1:54321/", "ports": {"api": 54320}}
    class Existing:
        def read(self):
            return state
    class Healthy:
        def __enter__(self):
            return SimpleNamespace(status=200)
        def __exit__(self, *args):
            pass
    monkeypatch.setattr(urllib.request, "build_opener", lambda *a: SimpleNamespace(open=lambda *a, **kw: Healthy()))
    monkeypatch.setattr(dev.subprocess, "Popen", lambda *a, **kw: pytest.fail("不得重复启动"))
    target = tmp_path / "state.json"
    args = SimpleNamespace(stop_file=None, platforms=["xzzd"], state_file=target, smoke_test=True)
    dev.reuse_runtime(args, Existing())
    assert json.loads(target.read_text())["web_url"] == state["web_url"]


def test_duplicate_start_missing_platform_reports_actionable_error():
    existing = SimpleNamespace(read=lambda: {"root": str(dev.ROOT), "status": "ready", "platforms": ["xzzd"]})
    args = SimpleNamespace(stop_file=None, platforms=["xzzd", "zykt"])
    with pytest.raises(SystemExit, match="zykt"):
        dev.reuse_runtime(args, existing)


def test_runtime_lock_releases_even_after_abrupt_process_exit(tmp_path):
    import subprocess
    path = tmp_path / "runtime.lock"
    code = "from runtime_lock import RuntimeLock; import os,sys; lock=RuntimeLock(sys.argv[1]); assert lock.acquire(); lock.publish({'status':'starting'}); os._exit(0)"
    subprocess.run([sys.executable, "-c", code, str(path)], cwd=Path(__file__).parent, check=True)
    replacement = RuntimeLock(path)
    assert replacement.acquire()
    replacement.publish({"status": "ready"})
    replacement.close()


def test_duplicate_window_stop_does_not_stop_original_runtime(tmp_path):
    stop = tmp_path / "stop"
    stop.touch()
    owner = RuntimeLock(tmp_path / "runtime.lock")
    assert owner.acquire()
    try:
        dev.reuse_runtime(SimpleNamespace(stop_file=stop), RuntimeLock(owner.path))
        duplicate = RuntimeLock(owner.path)
        assert not duplicate.acquire()
    finally:
        owner.close()

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

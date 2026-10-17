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


def test_dev_platform_flags_are_explicit_and_cannot_silently_disable_connectors():
    parser = dev.build_parser()
    assert parser.parse_args([]).platforms == ["xzzd"]
    assert parser.parse_args(["--platforms", "xzzd", "zykt"]).platforms == ["xzzd", "zykt"]
    assert parser.parse_args(["--no-platforms"]).platforms == []
    with pytest.raises(SystemExit):
        parser.parse_args(["--platforms"])


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


def test_base_python_avoids_building_a_venv_from_another_venv(monkeypatch, tmp_path):
    """The launcher may be invoked by another project's venv interpreter."""
    fake_base = tmp_path / "base"
    fake_base.mkdir()
    interpreter = fake_base / ("python.exe" if dev.os.name == "nt" else "bin/python3")
    interpreter.parent.mkdir(parents=True, exist_ok=True)
    interpreter.write_text("", encoding="utf-8")
    monkeypatch.setattr(dev.sys, "prefix", str(tmp_path / "borrowed-venv"))
    monkeypatch.setattr(dev.sys, "base_prefix", str(fake_base))
    monkeypatch.setattr(dev.sys, "executable", str(tmp_path / "borrowed-venv" / "python.exe"))
    assert dev.base_python() == str(interpreter)
    # Outside a venv the running interpreter is already the right one.
    monkeypatch.setattr(dev.sys, "prefix", str(fake_base))
    assert dev.base_python() == dev.sys.executable


def test_venv_is_accepted_when_usable_despite_a_nonzero_creator_exit(monkeypatch, tmp_path):
    """Windows AV locking pyvenv.cfg makes `python -m venv` exit 1 on a good venv."""
    api_dir = tmp_path / "api"
    interpreter = api_dir / ".venv" / ("Scripts/python.exe" if dev.os.name == "nt" else "bin/python")
    interpreter.parent.mkdir(parents=True, exist_ok=True)
    interpreter.write_text("", encoding="utf-8")
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if "venv" in command:
            return SimpleNamespace(returncode=1)  # created, but the creator reported failure
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(dev.subprocess, "run", fake_run)
    monkeypatch.setattr(dev, "base_python", lambda: "base-python")
    dev.ensure_project_venv(api_dir, attempts=3)
    assert sum(1 for command in calls if "venv" in command) == 1, "a usable venv must not be recreated"


def test_venv_creation_failure_is_reported_after_bounded_retries(monkeypatch, tmp_path):
    api_dir = tmp_path / "api"
    interpreter = api_dir / ".venv" / ("Scripts/python.exe" if dev.os.name == "nt" else "bin/python")
    interpreter.parent.mkdir(parents=True, exist_ok=True)
    interpreter.write_text("", encoding="utf-8")
    attempts = []

    def fake_run(command, **kwargs):
        if "venv" in command:
            attempts.append(command)
            return SimpleNamespace(returncode=1)
        return SimpleNamespace(returncode=1, stderr=b"broken interpreter")

    monkeypatch.setattr(dev.subprocess, "run", fake_run)
    monkeypatch.setattr(dev, "base_python", lambda: "base-python")
    monkeypatch.setattr(dev.time, "sleep", lambda _: None)
    with pytest.raises(SystemExit, match="虚拟环境"):
        dev.ensure_project_venv(api_dir, attempts=3)
    assert len(attempts) == 3, "creation must be retried a bounded number of times"


def test_missing_venv_interpreter_is_reported_without_crashing(monkeypatch, tmp_path):
    """Probing a not-yet-created interpreter must fail cleanly, not raise OSError."""
    usable, detail = dev.venv_usable(tmp_path / "absent" / "python.exe")
    assert usable is False
    assert "缺少解释器" in detail


def test_npm_environment_drops_outer_npm_markers_but_keeps_real_config():
    """A wrapper's `--allow-scripts` broke `npm ci`; real config must survive.

    Windows upper-cases environment names in `os.environ`, so the sanitizer must
    match case-insensitively — the uppercase keys here are the realistic ones.
    """
    source = {"NPM_CONFIG_ALLOW_SCRIPTS": "opencode-ai", "npm_command": "exec",
              "NPM_LIFECYCLE_EVENT": "npx", "npm_lifecycle_script": "dsh",
              "npm_config_registry": "https://registry.example/", "NPM_CONFIG_CACHE": "C:/cache",
              "PATH": "C:/bin"}
    cleaned = dev.npm_environment(source)
    for marker in ("NPM_CONFIG_ALLOW_SCRIPTS", "npm_command", "NPM_LIFECYCLE_EVENT", "npm_lifecycle_script"):
        assert marker not in cleaned
    assert not any(key.lower() == "npm_config_allow_scripts" for key in cleaned)
    assert cleaned["npm_config_registry"] == "https://registry.example/"
    assert cleaned["NPM_CONFIG_CACHE"] == "C:/cache"
    assert cleaned["PATH"] == "C:/bin"
    assert source["NPM_CONFIG_ALLOW_SCRIPTS"] == "opencode-ai", "the caller's environment must not be mutated"


def test_npm_environment_matches_the_real_process_environment(monkeypatch):
    """Guard against the Windows casing trap with the live environment."""
    monkeypatch.setenv("npm_config_allow_scripts", "opencode-ai")
    cleaned = dev.npm_environment()
    assert not any(key.lower() == "npm_config_allow_scripts" for key in cleaned)


def test_npm_install_passes_the_sanitized_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("npm_config_allow_scripts", "opencode-ai")
    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["cwd"] = kwargs.get("cwd")
        seen["env"] = kwargs.get("env")

    dev.npm_install("npm.cmd", tmp_path, fake_run)
    assert seen["command"] == ["npm.cmd", "ci"]
    assert seen["cwd"] == tmp_path
    assert "npm_config_allow_scripts" not in seen["env"]

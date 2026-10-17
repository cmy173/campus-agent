"""跨启动器互斥；锁随进程退出自动释放，保留浏览器登录目录。"""
from __future__ import annotations

import json
import os
from pathlib import Path


def default_lock_path():
    base = (Path(os.environ.get("LOCALAPPDATA") or Path.home()) if os.name == "nt"
            else Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share"))
    return base / "zjulearn" / "controlled-browser" / "workbench.lock"


class RuntimeLock:
    def __init__(self, path=None):
        self.path = Path(path) if path else default_lock_path()
        self.state_path = self.path.with_suffix(".json")
        self.handle = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        handle.seek(0, 2)
        if not handle.tell():
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self.handle = handle
        return True

    def publish(self, state):
        if self.handle is None:
            raise RuntimeError("Only the runtime owner may publish state")
        temporary = self.state_path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, self.state_path)

    def read(self):
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def close(self):
        if self.handle is not None:
            self.state_path.unlink(missing_ok=True)
            self.handle.close()
            self.handle = None

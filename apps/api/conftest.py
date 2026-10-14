"""pytest 引导：在导入 app.* 之前把数据库切到测试 SQLite（避免污染开发库 var/study.db）。"""
from __future__ import annotations

import os
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault("STUDY_DATABASE_URL", f"sqlite:///{(_REPO_ROOT / 'var' / 'test-study.db').as_posix()}")

"""pytest 引导：在导入 app.* 之前把数据库切到测试 SQLite（避免污染开发库 var/study.db）。

同时把凭据保险箱、Agent 数据目录与文件目录指向本次运行的临时目录：测试进程绝不能读到
本机真实保存的校园/WebVPN 凭据，更不能因此真的去请求学校接口。审计脚本（tools/audit_regression.py）
会先设置这些变量，setdefault 不会覆盖它。
"""
from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault("STUDY_DATABASE_URL", f"sqlite:///{(_REPO_ROOT / 'var' / 'test-study.db').as_posix()}")

_SCRATCH = Path(tempfile.gettempdir()) / f"campus-api-tests-{uuid.uuid4().hex}"
os.environ.setdefault("STUDY_SECRETS_DIR", str(_SCRATCH / "secrets"))
os.environ.setdefault("CAMPUS_AGENT_HOME", str(_SCRATCH / "agent"))
os.environ.setdefault("STUDY_FILES_DIR", str(_SCRATCH / "files"))
os.environ.setdefault("CAMPUS_DATA_DIR", str(_SCRATCH / "data"))

"""Start a source-aligned, local-only Agent API against disposable benchmark state.

The provider's non-secret configuration is copied into a fresh SQLite database.
Its encrypted key is referenced by provider ID from the existing DPAPI store; the
key is never read by this launcher, printed, copied, or written to benchmark data.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
API_ROOT = ROOT / "apps" / "api"
LEARNING_SRC = ROOT / "packages" / "learning" / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))
if str(LEARNING_SRC) not in sys.path:
    sys.path.insert(0, str(LEARNING_SRC))


OLD_NOTICE_READ_DESCRIPTION = (
    "读取本机校园缓存中的已展示学院通知正文、来源、附件链接和未读内容声明；"
    "record_id须来自campus.query/sync，不接受任意URL。"
)
BASELINE_CAMPUS_QUERY_DESCRIPTION = (
    "读取浙大校园缓存：courses课表、grades成绩、profile基本信息、notices校级摘要、"
    "college_notices已登记学院通知；优先缓存，已授权会话失效时仅安全刷新一次，不将缓存说成刚同步。"
)
BASELINE_CAMPUS_SYNC_DESCRIPTION = (
    "通过迁入的浙大连接器只读同步校园信息并保存缓存；需学校登录时明确返回AUTH_REQUIRED，"
    "失败保留旧缓存。只做只读教务查询。"
)


def apply_condition(condition):
    """Apply one benchmark-only schema-description factor to the isolated API."""
    if condition == "source":
        return
    from app.agent_bridge import tools
    if condition == "baseline-notice-read":
        schema, _description = tools.SPECS["campus.notice.read"]
        tools.SPECS["campus.notice.read"] = (schema, OLD_NOTICE_READ_DESCRIPTION)
    elif condition == "baseline-campus-args":
        for name, description in (("campus.query", BASELINE_CAMPUS_QUERY_DESCRIPTION),
                                  ("campus.sync", BASELINE_CAMPUS_SYNC_DESCRIPTION)):
            schema, _current_description = tools.SPECS[name]
            tools.SPECS[name] = (schema, description)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True, type=Path,
                        help="必须是新的空目录；仅在此目录写入测试数据库、Agent 状态和文件")
    parser.add_argument("--secrets-dir", required=True, type=Path,
                        help="现有 DPAPI 密钥目录；只按 provider ID 读取，不复制密钥")
    parser.add_argument("--provider-id", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument("--port", type=int, default=8017)
    parser.add_argument("--condition", choices=("source", "baseline-notice-read", "baseline-campus-args"), default="source",
                        help="控制条件仅回退目标工具描述；source 使用当前工作树")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    state_dir = args.state_dir.resolve()
    secret_dir = args.secrets_dir.resolve()
    parts = urlsplit(args.base_url)
    if not state_dir.exists():
        state_dir.mkdir(parents=True)
    if any(state_dir.iterdir()):
        raise SystemExit("state-dir 必须为空，防止复用用户数据库或缓存")
    if not secret_dir.is_dir():
        raise SystemExit("secrets-dir 不存在")
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password \
            or parts.query or parts.fragment:
        raise SystemExit("provider base-url 必须是无凭据、无 query/fragment 的 HTTP(S) URL")
    if not args.provider_id or len(args.provider_id) > 128:
        raise SystemExit("provider-id 无效")
    if not 1 <= args.timeout_seconds <= 300 or not 1024 <= args.port <= 65535:
        raise SystemExit("timeout 或 port 超出允许范围")

    db_path = state_dir / "benchmark.sqlite3"
    os.environ["STUDY_DATABASE_URL"] = "sqlite:///" + db_path.as_posix()
    os.environ["STUDY_FILES_DIR"] = str(state_dir / "files")
    os.environ["STUDY_SECRETS_DIR"] = str(secret_dir)
    os.environ["CAMPUS_AGENT_HOME"] = str(state_dir / "agent")
    os.environ["CAMPUS_BRIDGE_URL"] = f"http://127.0.0.1:{args.port}"
    os.environ["STUDY_WORKER_INPROC"] = "0"

    from app.db import Base, SessionLocal, engine
    from app.models import ProviderConfig
    from app import models as _models  # register all ORM tables before create_all
    from app import secrets_store

    Base.metadata.create_all(engine)
    if not secrets_store.has_key(args.provider_id):
        raise SystemExit("provider key is not configured in the referenced secret store")
    with SessionLocal() as db:
        db.add(ProviderConfig(id=args.provider_id, name="Isolated Campus Benchmark",
            protocol="openai_chat_completions", base_url=args.base_url, model=args.model,
            timeout_seconds=args.timeout_seconds, enabled=True))
        db.commit()

    apply_condition(args.condition)

    import uvicorn
    from app.main import app
    print(f"isolated_api_ready port={args.port} provider_id={args.provider_id} condition={args.condition}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning", access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

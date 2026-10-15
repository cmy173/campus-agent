"""测试夹具：会话级建表 + 装载契约样例 + TestClient。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="session")
def client():
    from app import models  # noqa: F401  注册模型到 Base.metadata
    from app.db import Base, SessionLocal, engine
    from app.main import app
    from app.seed import seed_all

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        seed_all(db)

    with TestClient(app) as c:
        yield c

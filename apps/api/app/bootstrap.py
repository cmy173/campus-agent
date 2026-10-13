"""个人模式初始化：仅补齐平台连接，不导入样例、不覆盖已有学习数据。"""
from sqlalchemy import select
from .db import SessionLocal
from .models import Connection


def ensure_connections(db):
    for platform, label in (("xzzd", "学在浙大"), ("zykt", "智云课堂")):
        existing = db.execute(select(Connection).where(Connection.platform == platform)).scalars().first()
        if existing is None:
            db.add(Connection(id=f"conn_{platform}", platform=platform, display_name=label,
                              status="service_unavailable", recovery_action=None,
                              last_error={"code": "DEPENDENCY_UNAVAILABLE", "message": "请启动本地连接器并检查连接",
                                          "retryable": False, "details": {"platform": platform}}))
    db.commit()


if __name__ == "__main__":
    with SessionLocal() as db:
        ensure_connections(db)
        from .course_identity import repair_duplicate_courses
        merged = repair_duplicate_courses(db)
        db.commit()
    print(f"平台连接已初始化；归并 {len(merged)} 组跨平台同课，资料保留。")

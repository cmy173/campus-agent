"""仅返回已同步场次的字幕，平台会话仍保留在本地连接器内。"""
from typing import Any
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..db import get_db
from ..errors import ApiError
from ..models import Recording, Connection, OWNER_RECORDING
from ..repo import get_or_404, refs_for
from ..connectors_registry import protocol_adapter
from ..sync_engine import AdapterMissing, _translate_protocol_error, record_check_failure

router = APIRouter(tags=["recordings"])


@router.get("/recordings/{recording_id}/transcript")
def get_transcript(recording_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    get_or_404(db, Recording, recording_id, "回放")
    ref = next((ref for ref in refs_for(db, OWNER_RECORDING, recording_id) if ref.platform == "zykt"), None)
    if ref is None:
        raise ApiError(501, "NOT_IMPLEMENTED", "该回放暂无可获取的字幕来源")
    connection = db.execute(select(Connection).where(Connection.platform == ref.platform)).scalar_one()
    if connection.status == "disabled":
        raise ApiError(409, "CONFLICT", "连接已停用")
    try:
        result = protocol_adapter(ref.platform, connection.id).get_transcript(ref.external_id)
        segments = result.get("segments")
        if not isinstance(segments, list):
            raise ApiError(502, "UPSTREAM_CHANGED", "字幕响应格式异常")
        # 仅投影字幕白名单字段，不把平台原始响应下发前端。
        return {"recording_id": recording_id, "language": result.get("language", "zh-CN"),
                "provenance": "platform", "segments": [
                    {key: segment[key] for key in ("start_seconds", "end_seconds", "text", "translated_text") if key in segment}
                    for segment in segments if isinstance(segment, dict)]}
    except AdapterMissing as exc:
        raise ApiError(503, "DEPENDENCY_UNAVAILABLE", str(exc)) from None
    except ApiError:
        raise
    except Exception as exc:
        error = _translate_protocol_error(exc)
        if error is None:
            raise
        record_check_failure(connection, error)
        db.commit()
        raise ApiError(502, error.code, error.message, retryable=error.retryable) from None

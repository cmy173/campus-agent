"""仅返回已同步场次的字幕，平台会话仍保留在本地连接器内。"""
from typing import Any
from fastapi import APIRouter, Depends
from sqlalchemy import select, or_
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


from typing import Literal
from pydantic import BaseModel, Field, ConfigDict
from ..models import Job, ProviderConfig, Course, utc_now
from ..repo import new_id


class SummaryInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    provider_id: str
    detail: Literal['brief', 'detailed'] = 'brief'
    transcript: str = Field(min_length=1, max_length=160000)


@router.get('/classroom/recordings')
def classroom_recordings(db: Session = Depends(get_db)):
    rows = db.execute(select(Recording, Course).join(Course, Course.id == Recording.course_id)
                      .where(Course.archived_at.is_(None), or_(Recording.started_at.is_(None), Recording.started_at <= utc_now()))
                      .order_by(Recording.started_at.desc(), Recording.id)).all()
    return {'items': [{**recording.to_contract(refs_for(db, OWNER_RECORDING, recording.id)),
                       'course_title': course.title, 'teacher': course.teacher}
                      for recording, course in rows
                      if any(ref.platform == 'zykt' for ref in refs_for(db, OWNER_RECORDING, recording.id))]}


@router.post('/recordings/{recording_id}/summary', status_code=202)
def create_summary(recording_id: str, payload: SummaryInput, db: Session = Depends(get_db)):
    recording = get_or_404(db, Recording, recording_id, '回放')
    course = get_or_404(db, Course, recording.course_id, '课程')
    if course.archived_at or not any(ref.platform == 'zykt' for ref in refs_for(db, OWNER_RECORDING, recording_id)):
        raise ApiError(400, 'VALIDATION_FAILED', '请选择自己已同步的智云课堂场次')
    provider = get_or_404(db, ProviderConfig, payload.provider_id, 'AI 服务')
    if provider.protocol != 'openai_chat_completions':
        raise ApiError(400, 'VALIDATION_FAILED', '该 API 协议不支持课堂总结')
    from ..secrets_store import get_key
    if not get_key(provider.id):
        raise ApiError(400, 'VALIDATION_FAILED', '请先在设置中填写该 AI 服务的密钥')
    if not payload.transcript.strip():
        raise ApiError(400, 'VALIDATION_FAILED', '没有可总结的课堂转写')
    frozen = {**payload.model_dump(), 'recording_id': recording.id, 'title': recording.title,
              'course_title': course.title, 'provider': {'base_url': provider.base_url,
              'model': provider.model, 'timeout_seconds': provider.timeout_seconds}}
    for job in db.scalars(select(Job).where(Job.type == 'classroom_summary', Job.status.in_(['queued', 'running']))):
        if job.payload == frozen:
            return {'job': job.to_contract()}
    job = Job(id=new_id(db, Job, 'job'), type='classroom_summary', status='queued',
              progress=0, payload=frozen, errors=[], result_refs=[])
    db.add(job)
    db.commit()
    return {'job': job.to_contract()}


@router.get('/classroom/summaries')
def summary_history(db: Session = Depends(get_db)):
    jobs = db.scalars(select(Job).where(Job.type == 'classroom_summary')
                      .order_by(Job.created_at.desc()).limit(100))
    return {'items': [{'job': job.to_contract(), **{key: (job.payload or {}).get(key)
                      for key in ('recording_id', 'title', 'course_title', 'detail', 'summary')}} for job in jobs]}

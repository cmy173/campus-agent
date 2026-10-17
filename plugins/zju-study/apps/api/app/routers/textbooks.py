"""本地教材版本、文件关联、习题提取与核对。"""
from __future__ import annotations
from urllib.parse import urlsplit, urlunsplit
import re

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..db import get_db
from ..errors import ApiError
from ..models import Course, Resource, ContentChunk, Textbook, TextbookExercise, TextbookSettings, utc_now
from ..repo import get_or_404, new_id
from ..textbook_content import extract_candidates, index_book
from ..textbook_rights import settings_view, check_book, saved_report

router = APIRouter(tags=['textbooks'])


class SettingsInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    rights_check_enabled: bool = True


@router.get('/preferences/textbooks')
def get_settings(db: Session = Depends(get_db)):
    return settings_view(db)


@router.put('/preferences/textbooks')
def put_settings(payload: SettingsInput, db: Session = Depends(get_db)):
    row = db.get(TextbookSettings, 1)
    if row is None:
        row = TextbookSettings(id=1)
        db.add(row)
    row.rights_check_enabled = payload.rights_check_enabled
    db.commit()
    return settings_view(db)


def serialize_book(db, book):
    return {**book.to_contract(), 'rights_check': saved_report(db, book)}


class ScopeInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    course_id: str | None = Field(default=None, min_length=1)


@router.get('/textbooks/catalogue')
def all_books(db: Session = Depends(get_db)):
    rows = db.scalars(select(Textbook).join(Course).where(Course.archived_at.is_(None))
                      .order_by(Textbook.course_id, Textbook.title))
    return {'items': [serialize_book(db, book) for book in rows]}


@router.post('/textbooks/rights-check')
def check_books(payload: ScopeInput, db: Session = Depends(get_db)):
    statement = select(Textbook).join(Course).where(Course.archived_at.is_(None))
    if payload.course_id:
        get_or_404(db, Course, payload.course_id, '课程')
        statement = statement.where(Textbook.course_id == payload.course_id)
    results = [{'textbook_id': book.id, 'title': book.title, 'result': check_book(db, book)}
               for book in db.scalars(statement)]
    db.commit()
    return {'items': results, 'enabled': settings_view(db)['rights_check_enabled']}


@router.post('/textbooks/{textbook_id}/rights-check')
def check_one_book(textbook_id: str, db: Session = Depends(get_db)):
    result = check_book(db, get_or_404(db, Textbook, textbook_id, '教材'))
    db.commit()
    return result


class BookInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: str = Field(min_length=1, max_length=512)
    author: str = Field(default='', max_length=256)
    edition: str = Field(default='', max_length=128)
    isbn: str = Field(default='', max_length=32)
    source_url: str = Field(default='', max_length=1024)
    resource_id: str | None = None
    confirmed: bool = False


def apply_book(db, book, payload):
    if not payload.title.strip():
        raise ApiError(400, 'VALIDATION_FAILED', '教材名称不能为空')
    if payload.confirmed and not (payload.isbn.strip() or (payload.author.strip() and payload.edition.strip())):
        raise ApiError(400, 'VALIDATION_FAILED', '确认教材版本需要 ISBN，或作者与版次')
    if payload.confirmed and payload.isbn.strip():
        isbn = re.sub(r'[\s-]', '', payload.isbn).upper()
        valid10 = bool(re.fullmatch(r'\d{9}[\dX]', isbn)) and sum((10 - i) * (10 if char == 'X' else int(char)) for i, char in enumerate(isbn)) % 11 == 0
        valid13 = bool(re.fullmatch(r'\d{13}', isbn)) and sum(int(char) * (1 if i % 2 == 0 else 3) for i, char in enumerate(isbn)) % 10 == 0
        if not (valid10 or valid13):
            raise ApiError(400, 'VALIDATION_FAILED', 'ISBN 校验未通过，请核对书籍版权页，或仅填写准确的作者与版次')
    if payload.resource_id:
        resource = get_or_404(db, Resource, payload.resource_id, '教材文件')
        if resource.course_id != book.course_id or resource.archived_at:
            raise ApiError(400, 'VALIDATION_FAILED', '教材文件必须属于当前课程且未归档')
    # 来源只记录出处，不保留学号/会话查询参数，也不自动访问任意 URL。
    source = urlsplit(payload.source_url)
    if payload.source_url and (source.scheme not in ('http', 'https') or not source.hostname or source.username is not None or source.password is not None):
        raise ApiError(400, 'VALIDATION_FAILED', '来源网址须为 HTTP/HTTPS 页面')
    if db.get(Textbook, book.id) is not None and any(
        getattr(book, key) != getattr(payload, key) for key in ('title', 'author', 'edition', 'isbn', 'resource_id')
    ):
        db.execute(update(TextbookExercise).where(TextbookExercise.textbook_id == book.id).values(confirmed=False))
    for key, value in payload.model_dump().items():
        setattr(book, key, value)
    book.title = payload.title.strip()
    book.source_url = urlunsplit((source.scheme, source.netloc, source.path, '', '')) if payload.source_url else ''
    book.updated_at = utc_now()


@router.get('/courses/{course_id}/textbooks')
def list_books(course_id: str, db: Session = Depends(get_db)):
    get_or_404(db, Course, course_id, '课程')
    return {'items': [serialize_book(db, book) for book in db.scalars(select(Textbook).where(Textbook.course_id == course_id))]}


@router.post('/courses/{course_id}/textbooks', status_code=201)
def add_book(course_id: str, payload: BookInput, db: Session = Depends(get_db)):
    get_or_404(db, Course, course_id, '课程')
    book = Textbook(id=new_id(db, Textbook, 'bok'), course_id=course_id)
    apply_book(db, book, payload)
    db.add(book)
    db.flush()
    if book.resource_id:
        index_book(db, book)
        check_book(db, book)
    db.commit()
    return book.to_contract()


@router.put('/textbooks/{textbook_id}')
def update_book(textbook_id: str, payload: BookInput, db: Session = Depends(get_db)):
    book = get_or_404(db, Textbook, textbook_id, '教材')
    apply_book(db, book, payload)
    db.flush()
    if book.resource_id:
        index_book(db, book)
        check_book(db, book)
    db.commit()
    return book.to_contract()


@router.post('/textbooks/{textbook_id}/auto-index')
def auto_index(textbook_id: str, db: Session = Depends(get_db)):
    result = index_book(db, get_or_404(db, Textbook, textbook_id, '教材'))
    db.commit()
    return result


class ExtractInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    chapter: int = Field(ge=1, le=999)
    start_page: int = Field(ge=1, le=2000)
    end_page: int = Field(ge=1, le=2000)

    @model_validator(mode='after')
    def range_check(self):
        if not self.start_page <= self.end_page <= self.start_page + 50:
            raise ValueError('请指定不超过 51 页的有效习题页范围')
        return self


@router.post('/textbooks/{textbook_id}/extract')
def extract(textbook_id: str, payload: ExtractInput, db: Session = Depends(get_db)):
    book = get_or_404(db, Textbook, textbook_id, '教材')
    resource = db.get(Resource, book.resource_id) if book.resource_id else None
    if not resource or resource.parse_status != 'ready':
        raise ApiError(409, 'CONFLICT', '请先关联并解析可读的教材 PDF；扫描件需要 OCR')
    chunks = list(db.scalars(select(ContentChunk).where(ContentChunk.resource_id == resource.id,
                                                       ContentChunk.resource_version == resource.version)))
    candidates = extract_candidates(chunks, payload.chapter, payload.start_page, payload.end_page)
    return {'items': candidates, 'resource_version': resource.version,
            'message': '候选正文需逐题核对章节、编号和公式，保存后才参与自动出卷。' if candidates else '这些页面未识别出明确编号；可在下方手动录入原题，不会让 AI 猜题。'}


class ExerciseInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    chapter: int = Field(ge=1, le=999)
    number: str = Field(pattern=r'^\d{1,3}$')
    text: str = Field(min_length=10, max_length=20000)
    page: int | None = Field(default=None, ge=1, le=2000)
    resource_version: int | None = Field(default=None, ge=1)
    confirmed: bool = True


@router.get('/textbooks/{textbook_id}/exercises')
def list_exercises(textbook_id: str, db: Session = Depends(get_db)):
    get_or_404(db, Textbook, textbook_id, '教材')
    rows = db.scalars(select(TextbookExercise).where(TextbookExercise.textbook_id == textbook_id)
                      .order_by(TextbookExercise.chapter, TextbookExercise.number))
    return {'items': [{key: getattr(row, key) for key in ExerciseInput.model_fields} for row in rows]}


@router.put('/textbooks/{textbook_id}/exercises')
def save_exercise(textbook_id: str, payload: ExerciseInput, db: Session = Depends(get_db)):
    book = get_or_404(db, Textbook, textbook_id, '教材')
    if not payload.text.strip() or len(payload.text.strip()) < 10:
        raise ApiError(400, 'VALIDATION_FAILED', '题目正文不足，不能只填写题号')
    if payload.resource_version is not None:
        resource = db.get(Resource, book.resource_id) if book.resource_id else None
        if not resource or resource.version != payload.resource_version:
            raise ApiError(409, 'CONFLICT', '教材文件版本已变化，请重新提取并核对')
    number = str(int(payload.number))
    if number == '0':
        raise ApiError(400, 'VALIDATION_FAILED', '题号从 1 开始')
    row = db.get(TextbookExercise, (textbook_id, payload.chapter, number))
    if row is None:
        row = TextbookExercise(textbook_id=textbook_id, chapter=payload.chapter, number=number)
        db.add(row)
    for key, value in payload.model_dump().items():
        setattr(row, key, number if key == 'number' else value)
    db.commit()
    return {'saved': True}

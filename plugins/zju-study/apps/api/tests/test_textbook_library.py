import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import config
from app.db import Base, get_db
from app.main import app
from app.models import Assignment, ContentChunk, Course, FileRecord, Resource
from app.review import resolve_selection, compute_fingerprint, notes_payload
from app.textbook_content import assignment_references


@pytest.fixture
def library(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'library.db'}", connect_args={'check_same_thread': False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(config, 'FILES_DIR', tmp_path / 'files')
    config.FILES_DIR.mkdir()
    monkeypatch.delenv('STUDY_WORKER_INPROC', raising=False)
    def override():
        with sessions() as db:
            yield db
    app.dependency_overrides[get_db] = override
    with sessions() as db:
        db.add_all([Course(id='crs_textbook', title='原子物理'), Course(id='crs_other', title='光学')])
        db.add(Resource(id='res_textbook', course_id='crs_textbook', title='教材：测试教材', kind='document',
                        version=1, parse_status='ready', access_level='full'))
        db.add(Resource(id='res_other', course_id='crs_other', title='其他课程教材', kind='document',
                        version=1, parse_status='ready'))
        db.add(ContentChunk(id='chk_textbook', resource_id='res_textbook', resource_version=1, ordinal=1,
                            text='1. 求质量为 m 的粒子能量，写出推导过程。\n2. 对比两种模型并说明适用条件。',
                            locator={'kind': 'page', 'page': 5}, provenance={}))
        db.add(Assignment(id='asg_textbook', course_id='crs_textbook', title='作业一', instructions='<p>第一章习题第1、2、3题</p>'))
        db.commit()
    try:
        with TestClient(app) as client:
            yield client, sessions
    finally:
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()


def add_book(client, title='原子物理', edition='第 5 版'):
    response = client.post('/api/v1/courses/crs_textbook/textbooks', json={
        'title': title, 'author': '测试作者', 'edition': edition, 'confirmed': True,
        'resource_id': 'res_textbook', 'source_url': 'https://zdbk.zju.edu.cn/course?su=private&token=secret'})
    assert response.status_code == 201, response.text
    book = response.json()
    assert '?' not in book['source_url']
    return book


def test_extract_confirm_and_freeze_only_referenced_exercises(library):
    client, sessions = library
    book = add_book(client)
    prefix = f"/api/v1/textbooks/{book['id']}"
    result = client.post(prefix + '/extract', json={'chapter': 1, 'start_page': 5, 'end_page': 5}).json()
    assert len(result['items']) == 2
    selection = {'assignments': [{'assignment_id': 'asg_textbook'}]}
    with sessions() as db:
        before, missing = resolve_selection(db, 'crs_textbook', selection)
        assert '求质量' not in before[0]['text']
        assert missing
    for item in result['items']:
        response = client.put(prefix + '/exercises', json={**item, 'resource_version': 1})
        assert response.status_code == 200, response.text
    with sessions() as db:
        after, missing = resolve_selection(db, 'crs_textbook', selection, notes_text='参考教材：原子物理')
        assert '求质量' in after[0]['text']
        assert '第1章第3题' in missing[0]['message']
        a = compute_fingerprint('crs_textbook', 'ai', 'v1', notes_payload(''), {}, after)
    item = result['items'][0]
    assert client.put(prefix + '/exercises', json={**item, 'text': '求质量为 m 的粒子动量，写出推导过程。', 'resource_version': 1}).status_code == 200
    with sessions() as db:
        changed, _ = resolve_selection(db, 'crs_textbook', selection)
        b = compute_fingerprint('crs_textbook', 'ai', 'v1', notes_payload(''), {}, changed)
        assert a != b
        other, missing = resolve_selection(db, 'crs_textbook', selection, notes_text='参考教材：另一位作者《另一本书》')
        assert '求质量' not in other[0]['text']
        assert missing


def test_ambiguous_edition_and_file_change_are_not_used(library):
    client, sessions = library
    first = add_book(client)
    second = add_book(client, edition='第 6 版')
    for book in (first, second):
        assert client.put(f"/api/v1/textbooks/{book['id']}/exercises", json={
            'chapter': 1, 'number': '1', 'text': '这是已经核对的测试原题，请计算实验结果。', 'resource_version': 1}).status_code == 200
    with sessions() as db:
        matched, _ = resolve_selection(db, 'crs_textbook', {'assignments': [{'assignment_id': 'asg_textbook'}]},
                                       notes_text='参考教材：原子物理，第 5 版')
        assert '已经核对的测试原题' in matched[0]['text']
        items, missing = resolve_selection(db, 'crs_textbook', {'assignments': [{'assignment_id': 'asg_textbook'}]})
        assert '已经核对的测试原题' not in items[0]['text']
        assert '存在多本教材匹配' in missing[0]['message']
        db.get(Resource, 'res_textbook').version = 2
        db.commit()
        items, missing = resolve_selection(db, 'crs_textbook', {'assignments': [{'assignment_id': 'asg_textbook'}]})
        assert '已经核对的测试原题' not in items[0]['text']
    assert client.put(f"/api/v1/textbooks/{first['id']}/exercises", json={
        'chapter': 1, 'number': '1', 'text': '这是已经核对的测试原题，请计算实验结果。', 'resource_version': 1}).status_code == 409


def test_metadata_requires_version_and_same_course(library):
    client, _ = library
    assert client.post('/api/v1/courses/crs_textbook/textbooks', json={'title': '教材', 'confirmed': True}).status_code == 400
    assert client.post('/api/v1/courses/crs_textbook/textbooks', json={'title': '教材', 'isbn': '123', 'confirmed': True}).status_code == 400
    assert client.post('/api/v1/courses/crs_textbook/textbooks', json={'title': '教材', 'resource_id': 'res_other'}).status_code == 400


def test_chapter_refs_do_not_guess():
    assert assignment_references('第四章习题第1、2和3题，截止时间 2026-10-13') == [(4, '1'), (4, '2'), (4, '3')]
    assert assignment_references('第二章习题第7-9题') == [(2, '7'), (2, '8'), (2, '9')]
    assert assignment_references('本周作业 1、2、3') == []


def test_online_textbook_features_are_removed_but_local_library_remains(library):
    client, sessions = library
    for path in ('/textbooks/sync', '/textbooks/source/open', '/courses/crs_textbook/textbooks/portal'):
        assert '/api/v1' + path not in app.openapi()['paths']
        assert client.post('/api/v1' + path, json={}).status_code in (404, 405)
    book = add_book(client)
    assert book['resource_id'] == 'res_textbook'
    from app import worker
    from app.models import Job
    old_job = Job(id='job_retired_books', type='textbook_sync', status='failed', progress=0,
                  payload={'action': 'acquire'}, errors=[], result_refs=[])
    with sessions() as db:
        db.add(old_job)
        db.commit()
        with pytest.raises(worker.JobFailed, match='自动读取教务网教材'):
            worker.handle_textbooks(db, old_job)
    assert client.post('/api/v1/jobs/job_retired_books/retry').status_code == 410


def test_local_pdf_preview_and_missing_converter(library, monkeypatch):
    client, sessions = library
    with sessions() as db:
        db.add_all([FileRecord(id='fil_abcdef', original_name='test.pdf', mime_type='application/pdf',
                              size_bytes=12, content_hash='abc'),
                    FileRecord(id='fil_abcdee', original_name='test.pptx',
                               mime_type='application/vnd.openxmlformats-officedocument.presentationml.presentation',
                               size_bytes=4, content_hash='def')])
        db.commit()
    (config.FILES_DIR / 'fil_abcdef').write_bytes(b'%PDF-test')
    (config.FILES_DIR / 'fil_abcdee').write_bytes(b'PKxx')
    assert client.post('/api/v1/files/fil_abcdef/preview').status_code == 200
    response = client.get('/api/v1/files/fil_abcdef/preview')
    assert response.headers['content-type'] == 'application/pdf'
    assert response.headers['content-disposition'].startswith('inline')
    monkeypatch.setattr('app.document_preview.converter_path', lambda: None)
    assert client.post('/api/v1/files/fil_abcdee/preview').status_code == 503

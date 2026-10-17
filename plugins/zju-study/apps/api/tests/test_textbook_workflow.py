import base64
import io
import zipfile

import pytest
from sqlalchemy import select

from test_textbook_library import library, add_book
from app import worker
from app.models import ContentChunk, Job, Resource, Textbook, TextbookExercise, TextbookRightsReport
from app.textbook_content import auto_candidates
from app.textbook_rights import classify_declarations
from app.errors import ApiError


def docx_bytes():
    paragraphs = ['第一章 原子结构', '习题', '1. 求一个测试粒子的能量，并写出完整推导过程。',
                  '2. 比较两种实验模型的适用范围并说明理由。', '参考答案', '1. 这里是答案，不能作为第二份题干。']
    body = ''.join(f'<w:p><w:r><w:t>{line}</w:t></w:r></w:p>' for line in paragraphs)
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        archive.writestr('[Content_Types].xml', '<Types/>')
        archive.writestr('word/document.xml', f'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>{body}</w:body></w:document>')
    return output.getvalue()


def test_rights_switch_is_persistent_advisory_and_version_bound(library):
    client, sessions = library
    book = add_book(client)
    with sessions() as db:
        db.get(ContentChunk, 'chk_textbook').text = '本教材许可声明 https://creativecommons.org/licenses/by/4.0/'
        db.commit()
    result = client.post(f"/api/v1/textbooks/{book['id']}/rights-check").json()
    assert result['state'] == 'declaration_found'
    assert '尚未核实' in result['message']
    assert result['evidence']
    assert client.put('/api/v1/preferences/textbooks', json={'rights_check_enabled': False}).status_code == 200
    assert client.get('/api/v1/preferences/textbooks').json()['rights_check_enabled'] is False
    assert client.post('/api/v1/textbooks/rights-check', json={}).json()['enabled'] is False
    assert client.get('/api/v1/courses/crs_textbook/textbooks').json()['items'][0]['rights_check']['state'] == 'disabled'
    assert client.put('/api/v1/preferences/textbooks', json={'rights_check_enabled': True}).status_code == 200
    with sessions() as db:
        db.get(Resource, 'res_textbook').version = 2
        db.commit()
    assert client.get('/api/v1/courses/crs_textbook/textbooks').json()['items'][0]['rights_check']['state'] == 'stale'
    assert classify_declarations('本书免费下载，仅供个人学习')['state'] == 'unknown'
    assert classify_declarations('All rights reserved. https://creativecommons.org/licenses/by/4.0/')['state'] == 'conflicting'


def test_auto_index_requires_real_exercise_section_and_handles_ambiguity():
    chunks = [ContentChunk(id='a', ordinal=1, text='第一章 原子结构\n1. 此处为普通正文编号，不应当作为习题。\n习题\n1. 求粒子的能量并写出完整推导过程。\n2. 比较两种模型并说明适用范围。', locator={'page': 5}),
              ContentChunk(id='b', ordinal=2, text='参考答案\n1. 这是答案，不是重复题干。\n第二章 光谱\n习题\n1. 分析光谱变化并说明原因及适用条件。', locator={'page': 9})]
    items, ambiguous = auto_candidates(chunks)
    assert [(item['chapter'], item['number']) for item in items] == [(1, '1'), (1, '2'), (2, '1')]
    assert not ambiguous
    assert '普通正文' not in items[0]['text']
    chunks.append(ContentChunk(id='c', ordinal=3, text='1. 第二章又出现相同题号，不应该静默覆盖。', locator={'page': 10}))
    items, ambiguous = auto_candidates(chunks)
    assert (2, '1') not in [(item['chapter'], item['number']) for item in items]
    assert ambiguous == ['第2章第1题重复出现']


def test_manual_upload_book_version_change_stops_stale_association(library, monkeypatch):
    client, sessions = library
    book = add_book(client)
    monkeypatch.setattr(worker, 'SessionLocal', sessions)
    import json
    response = client.post('/api/v1/imports', data={'payload': json.dumps({'course_id': 'crs_textbook', 'textbook_id': book['id'], 'kind': 'document', 'title': '教材：原子物理'})}, files={'files': ('book.docx', docx_bytes(), 'application/octet-stream')})
    assert response.status_code == 202, response.text
    job_id = response.json()['job']['id']
    with sessions() as db:
        db.get(Textbook, book['id']).edition = '另一版'
        db.commit()
    assert worker.process_next_job()
    with sessions() as db:
        job = db.get(Job, job_id)
        assert job.status == 'failed'
        assert job.errors[0]['code'] == 'TEXTBOOK_CHANGED'
        assert db.get(Textbook, book['id']).resource_id == 'res_textbook'



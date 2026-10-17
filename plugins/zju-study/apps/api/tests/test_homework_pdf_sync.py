"""作业仅有 PDF：平台元数据替身 → 真 PDF 下载落盘 → 真解析 → 出卷材料。"""
import hashlib
import io

from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
from sqlalchemy import select, func

from test_textbook_library import library
from app import worker
from app.models import Assignment, Connection, Resource, SourceRef
from app.review import resolve_selection, list_materials
from app.sync_engine import apply_sync_result
from app.repo import find_source_owner
from app.build_engine import _load_resources
from connectors.adapters.xzzd import XzzdAdapter
from connectors.tests.fakes import ScriptedTransport, json_response
from connectors.tests.validate_samples import validator


def pdf_bytes():
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                             NameObject('/Subtype'): NameObject('/Type1'),
                             NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): font})})
    stream = DecodedStreamObject()
    stream.set_data(b'BT /F1 12 Tf 40 700 Td (Homework question: compute the complex integral and explain every step. State the assumptions and derive the final expression using the residue theorem.) Tj ET')
    page[NameObject('/Contents')] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


class DownloadTransport(ScriptedTransport):
    def __init__(self, responses, data):
        super().__init__(responses)
        self.data = data

    def download(self, url, dest, *, max_bytes, chunk_size):
        assert len(self.data) <= max_bytes
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.data)
        return len(self.data), hashlib.sha256(self.data).hexdigest()


def test_two_pdf_only_assignments_are_parsed_and_used_without_empty_body_warning(library, tmp_path, monkeypatch):
    _, sessions = library
    monkeypatch.setattr(worker, 'SessionLocal', sessions)
    stage = tmp_path / 'stage'
    monkeypatch.setenv('STUDY_CONNECTOR_DOWNLOAD_DIR', str(stage))
    monkeypatch.setenv('STUDY_FILE_LIMITS_PATH', str(tmp_path / 'limits.json'))
    data = pdf_bytes()
    responses = [json_response({'pages': 1, 'homework_activities': [
        {'id': 12, 'title': '第二次作业', 'data': {'description': None}},
        {'id': 13, 'title': '第三次作业', 'data': {'description': None}},
    ]})]
    for number in (12, 13):
        responses.append(json_response({'activity': {'id': number, 'data': {'description': None}, 'uploads': [
            {'id': number * 10, 'name': f'homework-{number}.pdf', 'size': len(data), 'type': 'application/pdf',
             'allow_download': True, 'origin_allow_download': True},
        ]}}))
    responses += [json_response({'url': 'https://media.example.invalid/test.pdf'}) for _ in range(2)]
    adapter = XzzdAdapter('conn_pdf_test', transport=DownloadTransport(responses, data), download_dir=stage)
    envelope = adapter.sync_course('crs_textbook', kinds=['assignments'])
    assert not list(validator('sync-result.schema.json').iter_errors(envelope))
    assert len(envelope['upserts']['resources']) == 2
    with sessions() as db:
        connection = Connection(id='conn_pdf_test', platform='xzzd', display_name='PDF test', status='ok')
        db.add(connection)
        db.commit()
        for _ in range(2):
            stats = apply_sync_result(db, connection=connection, course_id='crs_textbook', envelope=envelope)
            assert not stats.errors
            db.commit()
        assert db.scalar(select(func.count()).select_from(Assignment).where(Assignment.title.in_(['第二次作业', '第三次作业']))) == 2
        for item in envelope['upserts']['resources']:
            source = item['source_ref']
            rid = find_source_owner(db, 'resource', 'xzzd', source['external_id'])
            resource = db.get(Resource, rid)
            outcome = adapter.fetch_resource(source['external_id'])
            assert outcome['status'] == 'ok'
            payload = {'status': 'ok', 'filename': adapter._resources[source['external_id']]['file_name'],
                       'local_file': outcome['handle']['ref'], 'size': outcome['handle']['size'],
                       'sha256': outcome['handle']['sha256']}
            worker._fetch_synced_document(db, resource, source, lambda _rid, payload=payload: payload)
    for _ in range(10):
        if not worker.process_next_job():
            break
    with sessions() as db:
        assignments = list(db.scalars(select(Assignment).where(Assignment.title.in_(['第二次作业', '第三次作业']))))
        assert all(not assignment.instructions and len(assignment.attachment_resource_ids) == 1 for assignment in assignments)
        selection = {'assignments': [{'assignment_id': assignment.id, 'include_attachments': True} for assignment in assignments]}
        materials, missing = resolve_selection(db, 'crs_textbook', selection)
        assert not missing
        assert len(materials) == 2
        assert all(item['kind'] == 'attachment_text' and 'residue theorem' in item['text'] for item in materials)
        assert {item['assignment_id'] for item in materials} == {assignment.id for assignment in assignments}
        rows, _ = list_materials(db, 'crs_textbook', None, 100)
        assert all(row['attachments'][0]['usable'] for row in rows if row['id'] in {assignment.id for assignment in assignments})
        assert not _load_resources(db, 'crs_textbook')  # PDF 随作业使用，不额外制造阅读任务。


def test_invalid_or_cross_course_attachment_does_not_erase_existing_assignment(library):
    _, sessions = library
    from datetime import datetime, timezone
    with sessions() as db:
        original = db.get(Assignment, 'asg_textbook')
        original.attachment_resource_ids = ['res_textbook']
        db.add(SourceRef(owner_type='resource', owner_id='res_other', platform='xzzd', external_id='other-course-pdf',
                         observed_at=datetime.now(timezone.utc), seq=0))
        db.commit()
        from app.sync_engine import _upsert_assignment, _BadItem
        import pytest
        item = {'title': 'forged', 'attachment_external_ids': ['other-course-pdf'],
                'source_ref': {'platform': 'xzzd', 'external_id': '12', 'observed_at': '2026-10-16T00:00:00Z'}}
        with pytest.raises(_BadItem, match='不属于当前课程'):
            _upsert_assignment(db, platform='xzzd', course_id='crs_textbook', item=item)
        assert original.attachment_resource_ids == ['res_textbook']

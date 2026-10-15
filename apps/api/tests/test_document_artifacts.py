"""Verify actual renderer outputs, data types and source preservation."""
import importlib.util
import io
from zipfile import ZipFile
from datetime import datetime

import pytest
from openpyxl import load_workbook
from pypdf import PdfReader

from app.config import REPO_ROOT
from test_agent_gateway import gateway, conversation  # noqa: F401

script = REPO_ROOT / 'apps/agent-runtime/skills/document-artifacts/scripts/export_documents.py'
module_spec = importlib.util.spec_from_file_location('document_exporter', script)
renderer = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(renderer)


def document():
    return {'title': '校园资料整理', 'subtitle': '合成资料验收', 'sources': ['实验说明，原始附件'],
        'sections': [{'heading': '任务说明', 'paragraphs': ['请保留原始数据和真实截止时间。'],
                      'table': {'columns': ['资料', '页数', '截止时间'], 'rows': [['实验说明', 0, '未知'], ['课程讲义', 12, '2026-10-20']]}}],
        'sheets': [{'name': '资料清单', 'columns': ['资料', '页数', '日期'],
                    'rows': [['=不是公式', 0, {'date': '2026-10-20'}], ['课程讲义', 12, None],
                             ['合计', {'formula': '=SUM(B5:B6)'}, '未知']]}]}


def test_pdf_pagination_and_xlsx_types_and_preserved_outputs(tmp_path):
    spec = document()
    spec['sections'][0]['table']['rows'] += [[f'分页资料-{n}', n, '未知'] for n in range(100)]
    result = renderer.export(spec, tmp_path, '资料整理')
    assert len(result['files']) == 3
    pdf = PdfReader(tmp_path / '资料整理.pdf')
    text = '\n'.join(page.extract_text() for page in pdf.pages)
    assert len(pdf.pages) > 1 and all(value in text for value in ['校园资料整理', '分页资料-99', '实验说明', '未知'])
    book = load_workbook(tmp_path / '资料整理.xlsx', data_only=False)
    try:
        sheet = book['资料清单']
        assert sheet['A5'].value == '=不是公式' and sheet['A5'].data_type == 's'
        assert sheet['B5'].value == 0 and sheet['B6'].value == 12
        assert sheet['C5'].value == datetime(2026, 10, 20) and sheet['C5'].number_format == 'yyyy-mm-dd'
        assert sheet['B7'].value == '=SUM(B5:B6)' and sheet['B7'].data_type == 'f'
        assert sheet['C6'].value is None
    finally:
        book.close()
    before = (tmp_path / '资料整理.pdf').read_bytes()
    with pytest.raises(FileExistsError):
        renderer.export(spec, tmp_path, '资料整理')
    assert (tmp_path / '资料整理.pdf').read_bytes() == before


def test_invalid_table_fails_before_creating_outputs(tmp_path):
    spec = document()
    spec['sheets'][0]['rows'][0].append('错列')
    with pytest.raises(ValueError, match='columns'):
        renderer.export(spec, tmp_path, 'bad')
    assert not list(tmp_path.iterdir())


def test_uploaded_spreadsheet_preview_keeps_zero_dates_and_formulas(gateway, tmp_path):
    client, runtime, _, _ = gateway
    cid = conversation(client)
    renderer.export(document(), tmp_path / 'generated', 'preview', formats=['xlsx'])
    with (tmp_path / 'generated/preview.xlsx').open('rb') as stream:
        uploaded = client.post(f'/api/v1/agent/conversations/{cid}/files', files={'file': ('preview.xlsx', stream)},
                               headers={'Idempotency-Key': 'sheet-upload'})
    assert uploaded.status_code == 201, uploaded.text
    fid = uploaded.json()['id']
    preview = client.get(f'/api/v1/agent/conversations/{cid}/files/{fid}/preview').json()
    assert preview['kind'] == 'spreadsheet' and preview['has_formulas']
    rows = preview['sheets'][0]['rows']
    assert rows[4] == ['=不是公式', 0, '2026-10-20T00:00:00']
    assert rows[6][1] == '=SUM(B5:B6)'
    downloaded = client.get(f'/api/v1/agent/conversations/{cid}/files/{fid}/content')
    assert downloaded.content == (tmp_path / 'generated/preview.xlsx').read_bytes()


def test_malformed_spreadsheet_reports_preview_error_and_preserves_download(gateway):
    client, _, _, _ = gateway
    cid = conversation(client)
    broken = io.BytesIO()
    with ZipFile(broken, 'w') as archive:
        archive.writestr('[Content_Types].xml', 'not XML')
    content = broken.getvalue()
    uploaded = client.post(f'/api/v1/agent/conversations/{cid}/files', files={'file': ('broken.xlsx', content)},
                           headers={'Idempotency-Key': 'broken-sheet'})
    assert uploaded.status_code == 201
    path = f"/api/v1/agent/conversations/{cid}/files/{uploaded.json()['id']}"
    preview = client.get(path + '/preview')
    assert preview.status_code == 400 and preview.json()['error']['code'] == 'FILE_PREVIEW_FAILED'
    assert client.get(path + '/content').content == content

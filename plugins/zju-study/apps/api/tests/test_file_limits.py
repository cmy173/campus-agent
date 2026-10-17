import hashlib

import pytest

from test_textbook_library import library
from app import config, storage
from app.errors import ApiError
from connectors.file_limits import limit_bytes


def test_limits_persist_and_download_is_independent_of_parse(library, tmp_path, monkeypatch):
    monkeypatch.setenv('STUDY_FILE_LIMITS_PATH', str(tmp_path / 'limits.json'))
    client, _ = library
    values = {'download_mb': 1024, 'upload_mb': 512, 'parse_mb': 128}
    assert client.put('/api/v1/preferences/file-limits', json=values).status_code == 200
    assert client.get('/api/v1/preferences/file-limits').json() == values
    assert limit_bytes('download', 64) == 1024 * 1024 * 1024
    assert config.parse_limit_bytes() == 128 * 1024 * 1024
    assert config.upload_limit_bytes() == 512 * 1024 * 1024
    assert client.put('/api/v1/preferences/file-limits', json={**values, 'parse_mb': 0}).status_code == 400
    assert client.get('/api/v1/preferences/file-limits').json() == values


def test_staged_transfer_checks_hash_and_rejects_paths_without_losing_source(tmp_path, monkeypatch):
    monkeypatch.setenv('STUDY_FILE_LIMITS_PATH', str(tmp_path / 'limits.json'))
    stage = tmp_path / 'downloads'
    stage.mkdir()
    monkeypatch.setenv('STUDY_CONNECTOR_DOWNLOAD_DIR', str(stage))
    monkeypatch.setattr(config, 'FILES_DIR', tmp_path / 'files')
    data = b'%PDF-1.7\n' + b'x' * (2 * 1024 * 1024)
    source = stage / 'test.pdf'
    source.write_bytes(data)
    payload = {'local_file': 'test.pdf', 'size': len(data), 'sha256': 'sha256:' + hashlib.sha256(data).hexdigest()}
    saved = storage.store_connector_file('fil_123456', payload, display_name='test.pdf', mime_type='application/pdf')
    assert saved.size_bytes == len(data)
    assert storage.blob_path('fil_123456').read_bytes() == data
    with pytest.raises(ApiError, match='哈希'):
        storage.store_connector_file('fil_123457', {**payload, 'sha256': 'wrong'}, display_name='test.pdf', mime_type='application/pdf')
    assert not storage.blob_path('fil_123457').exists()
    assert not storage.blob_path('fil_123457').with_suffix('.part').exists()
    with pytest.raises(ApiError, match='句柄'):
        storage.store_connector_file('fil_123458', {**payload, 'local_file': '../test.pdf'}, display_name='test.pdf', mime_type='application/pdf')
    assert source.exists()

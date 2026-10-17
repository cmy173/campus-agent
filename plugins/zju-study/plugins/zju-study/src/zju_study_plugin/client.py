"""Only invoke the configured local study API; never export browser credentials."""
from __future__ import annotations
import os
import re
import uuid
import json
import mimetypes
from pathlib import Path
from urllib.parse import urlsplit
import requests


class StudyClient:
    def __init__(self, base_url=None, session=None):
        url = (base_url or os.getenv('ZJU_STUDY_API_URL', 'http://127.0.0.1:8000/api/v1')).rstrip('/')
        parsed = urlsplit(url)
        if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost', '::1') or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path != '/api/v1':
            raise ValueError('ZJU_STUDY_API_URL must be a loopback http://host:port/api/v1 address')
        self.base_url = url
        self.session = session or requests.Session()
        self.session.trust_env = False

    def call(self, spec, arguments):
        if spec['name'] == 'study_import_file':
            return self.upload(arguments)
        args = dict(arguments)
        path = spec['path']
        query = {}
        body = None
        headers = {}
        for name, field in spec.get('fields', {}).items():
            value = args.get(name, field.get('default'))
            if value is None:
                continue
            where = field.get('where', 'query')
            if where == 'path':
                if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value):
                    raise ValueError('Invalid object ID')
                path = path.replace('{' + name + '}', value)
            elif where == 'body':
                body = value
            elif where == 'header':
                headers['Idempotency-Key'] = value
            else:
                query[name] = value
        if spec['method'] != 'GET' and 'Idempotency-Key' not in headers:
            headers['Idempotency-Key'] = 'plugin-' + uuid.uuid4().hex
        try:
            response = self.session.request(spec['method'], self.base_url + path, params=query,
                                           json=body, headers=headers, timeout=(3,120), allow_redirects=False)
        except requests.RequestException:
            raise RuntimeError('BACKEND_UNAVAILABLE: start the local study backend and check its port') from None
        if 300 <= response.status_code < 400:
            raise RuntimeError('BACKEND_REDIRECT: local API redirects are not followed')
        if not response.ok:
            try:
                error = response.json().get('error', {})
                message = str(error.get('message', 'Local API request failed'))[:500]
                code = str(error.get('code', 'BACKEND_ERROR'))
            except ValueError:
                code, message = 'BACKEND_ERROR', 'Local API returned an unexpected response'
            raise RuntimeError(f'{code}: {message}')
        if len(response.content) > 4 * 1024 * 1024:
            raise RuntimeError('RESPONSE_TOO_LARGE: use pagination or select fewer materials')
        if response.status_code == 204:
            return {'ok': True}
        if response.headers.get('content-type', '').startswith('application/json'):
            return response.json()
        return {'text': response.text}


    def upload(self, args):
        course_id = args['course_id']
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', course_id):
            raise ValueError('Invalid course ID')
        path = Path(args['file_path']).expanduser()
        if not path.is_absolute() or not path.is_file():
            raise ValueError('Select an existing file by absolute path')
        if path.suffix.lower() not in ('.pdf','.ppt','.pptx','.docx','.zip') or path.stat().st_size > 100*1024*1024:
            raise ValueError('Supported input is PDF/PPT/PPTX/DOCX/ZIP, at most 100 MiB')
        title = args.get('title') or path.stem
        if args.get('purpose') == 'reference':
            title = '历年卷参考：' + title.removeprefix('历年卷参考：')
        payload = {'course_id':course_id, 'title':title, 'kind':'document'}
        if args.get('textbook_id'):
            payload['textbook_id'] = args['textbook_id']
        try:
            with path.open('rb') as stream:
                response = self.session.request('POST', self.base_url+'/imports',
                    data={'payload':json.dumps(payload,ensure_ascii=False)},
                    files=[('files',(path.name,stream,mimetypes.guess_type(path.name)[0] or 'application/octet-stream'))],
                    headers={'Idempotency-Key':args.get('idempotency_key') or 'plugin-'+uuid.uuid4().hex},
                    timeout=(3,120),allow_redirects=False)
        except requests.RequestException:
            raise RuntimeError('BACKEND_UNAVAILABLE: local material upload failed') from None
        if not response.ok or 300 <= response.status_code < 400:
            raise RuntimeError('IMPORT_FAILED: check the local API and selected file')
        return response.json()

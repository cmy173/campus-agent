import asyncio
import json
import os
from pathlib import Path
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import pytest
import requests

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'plugins/zju-study/src'))
from zju_study_plugin.client import StudyClient
from zju_study_plugin.catalogue import TOOLS
from zju_study_plugin.server import build_server


def test_all_tools_have_real_backend_routes():
    import yaml
    api=yaml.safe_load((ROOT/'contracts/openapi/openapi.yaml').read_text(encoding='utf-8'))['paths']
    for tool in TOOLS:
        assert tool['method'].lower() in api['/api/v1'+tool['path']], tool['name']
    assert len({tool['name'] for tool in TOOLS}) == len(TOOLS)
    tools=asyncio.run(build_server().list_tools())
    summary=next(tool for tool in tools if tool.name=='study_summarize_class')
    assert set(summary.inputSchema['required']) == {'recording_id','request'}
    assert not summary.annotations.readOnlyHint
    assert next(tool for tool in tools if tool.name=='study_transcript').annotations.readOnlyHint


@pytest.mark.parametrize('url',['https://example.org/api/v1','http://user:pass@localhost/api/v1','http://127.0.0.1/api/v1?key=secret','http://localhost/other'])
def test_refuses_nonlocal_or_credential_urls(url):
    with pytest.raises(ValueError): StudyClient(url)


def test_refuses_object_id_path_escape():
    tool=next(tool for tool in TOOLS if tool['name']=='study_job')
    with pytest.raises(ValueError): StudyClient().call(tool,{'job_id':'../ai/providers'})


def test_backend_unavailable_has_actionable_error(monkeypatch):
    client=StudyClient()
    monkeypatch.setattr(client.session,'request',lambda *a,**kw: (_ for _ in ()).throw(requests.ConnectionError('private details')))
    with pytest.raises(RuntimeError,match='BACKEND_UNAVAILABLE') as error: client.call(next(tool for tool in TOOLS if tool['name']=='study_status'),{})
    assert 'private details' not in str(error.value)
    assert client.session.trust_env is False


def test_real_stdio_handshake_list_and_calls_without_personal_data():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    calls=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_GET(self):
            calls.append(self.path)
            data={'status':'ok','version':'test'} if self.path.endswith('/health') else {'items':[],'next_cursor':None}
            encoded=json.dumps(data).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(encoded)));self.end_headers();self.wfile.write(encoded)
    http=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
    async def exercise():
        params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'tools/agent_plugin.py')],
            env={'ZJU_STUDY_API_URL':f'http://127.0.0.1:{http.server_port}/api/v1','PYTHONIOENCODING':'utf-8'})
        async with stdio_client(params) as (reader,writer):
            async with ClientSession(reader,writer) as client:
                await client.initialize()
                tools=await client.list_tools();assert len(tools.tools)==len(TOOLS)
                response=await client.call_tool('study_status',{})
                assert not response.isError
                assert response.structuredContent['status']=='ok'
                response=await client.call_tool('study_courses',{'limit':2})
                assert not response.isError
                assert response.structuredContent['items']==[]
                response=await client.call_tool('study_courses',{'limit':0})
                assert response.isError
    try: asyncio.run(exercise())
    finally: http.shutdown();http.server_close();thread.join(timeout=2)
    assert any('limit=2' in path for path in calls)
    assert not any('limit=0' in path for path in calls)


def test_manifest_and_examples_are_valid():
    import jsonschema
    manifest=json.loads((ROOT/'plugin.json').read_text(encoding='utf-8'))
    schema=json.loads((ROOT/'plugins/zju-study/plugin.schema.json').read_text(encoding='utf-8'))
    jsonschema.validate(manifest,schema)
    examples=json.loads((ROOT/'plugins/zju-study/examples.json').read_text(encoding='utf-8'))
    from app.schemas import ReviewRequestIn
    ReviewRequestIn.model_validate(examples['review_generate']['request'])
    assert (ROOT/manifest['interfaces']['mcp']['entrypoint']).is_file()
    assert (ROOT/manifest['host_instructions']).is_file()


def test_upload_uses_local_multipart_and_marks_reference(tmp_path):
    path=tmp_path/'reference.pdf';path.write_bytes(b'%PDF-1.4 synthetic fixture')
    seen={}
    class Session:
        def request(self,method,url,**kwargs):
            seen.update(method=method,url=url,**kwargs)
            assert kwargs['files'][0][1][1].read().startswith(b'%PDF-')
            class Response:
                ok=True
                status_code=202
                def json(self):return {'job':{'id':'job_fixture'}}
            return Response()
    client=StudyClient(session=Session())
    result=client.upload({'course_id':'crs_fixture','file_path':str(path),'purpose':'reference'})
    assert result['job']['id']=='job_fixture'
    assert seen['url']=='http://127.0.0.1:8000/api/v1/imports'
    assert json.loads(seen['data']['payload'])['title'].startswith('历年卷参考：')
    assert seen['allow_redirects'] is False

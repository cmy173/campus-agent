"""Real API + real Harness + controlled local model for browser acceptance.
Only a fresh UUID workspace is used. No school login, personal DB or paid model.

The bench is self-expiring: it stops on the printed stop-file, after `--ttl-seconds`
(default 1h) or when any service dies. Stopping it releases 15173/18080/19191 — a bench
that outlives its terminal is what steals the workbench launcher's ports.

    python tools/round2_testbench.py [--ttl-seconds 1800] [--stop-file PATH]
"""
from pathlib import Path
import argparse, json, os, re, subprocess, sys, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
ROOT = Path(__file__).resolve().parents[1]
_parser = argparse.ArgumentParser(add_help=True)
_parser.add_argument('--ttl-seconds', type=int, default=3600,
                     help='stop the bench after this many seconds (0 disables)')
_parser.add_argument('--stop-file', type=Path, default=None, help='stop when this file appears')
_args = _parser.parse_args()
WORK = ROOT / '.test-tmp' / ('round2-browser-' + uuid.uuid4().hex)
STOP_FILE = _args.stop_file or (WORK / 'stop')
WORK.mkdir(parents=True)
for name in ('files', 'secrets', 'agent', 'workspace', 'tmp'): (WORK/name).mkdir()
env = dict(os.environ, PYTHONUTF8='1', PYTHONPATH=str(ROOT)+';'+str(ROOT/'packages/learning/src'),
    STUDY_DATABASE_URL='sqlite:///'+(WORK/'domain.sqlite').as_posix(), STUDY_FILES_DIR=str(WORK/'files'),
    STUDY_SECRETS_DIR=str(WORK/'secrets'), CAMPUS_AGENT_HOME=str(WORK/'agent'), CAMPUS_WORKSPACE_DIR=str(WORK/'workspace'),
    CAMPUS_DATA_DIR=str(WORK/'campus'), STUDY_WORKER_INPROC='0', TEMP=str(WORK/'tmp'), TMP=str(WORK/'tmp'),
    CAMPUS_BRIDGE_URL='http://127.0.0.1:18080', CAMPUS_WEB_ORIGINS='http://127.0.0.1:15173',
    VITE_API_MODE='real', VITE_API_BASE_URL='', VITE_PROXY_TARGET='http://127.0.0.1:18080', VITE_AGENT_TEST_DATA='1')
requests_seen=[]
class Model(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))))
        messages=body.get('messages',[])
        last_user=next((m.get('content','') for m in reversed(messages) if m.get('role')=='user'),'')
        tools=[m for m in messages if m.get('role')=='tool']
        requests_seen.append({'index':len(requests_seen)+1,'message_count':len(messages),'tool_result_count':len(tools), 'tool_names':[s.get('function',{}).get('name') for s in body.get('tools',[])]})
        (WORK/'model-metrics.json').write_text(json.dumps(requests_seen,ensure_ascii=False,indent=2),encoding='utf-8')
        if not tools:
            # The trusted server context carries the exact per-turn attachment path;
            # the controlled model reads it like a real agent would.
            context_text = messages[0].get('content','') if messages else ''
            match=re.search(r'"path":\s*"([^"]+)"', context_text)
            if match:
                call={'id':'fixture_attachment_read','type':'function','function':{'name':'read','arguments':json.dumps({'file_path':match.group(1).replace('\\\\','\\')})}}
            else:
                call={'id':'fixture_course_read','type':'function','function':{'name':'campus_courses_list','arguments':'{}'}}
            message={'role':'assistant','content':None,'tool_calls':[call]}
            finish='tool_calls'
        else:
            message={'role':'assistant','content':'【合成模型验收，不是校园实测】已读取本地课程资料：\n'+tools[-1].get('content','')}
            finish='stop'
        result={'id':'controlled-round2','object':'chat.completion','choices':[{'index':0,'message':message,'finish_reason':finish}],'usage':{'prompt_tokens':1,'completion_tokens':1,'total_tokens':2}}
        raw=json.dumps(result,ensure_ascii=False).encode()
        self.send_response(200); self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
server=ThreadingHTTPServer(('127.0.0.1',19191),Model)
threading.Thread(target=server.serve_forever,daemon=True).start()
python=ROOT/'apps/api/.venv/Scripts/python.exe'
code="""from app.db import Base,engine,SessionLocal
from app import models as m
Base.metadata.create_all(engine)
with SessionLocal() as db:
 db.add(m.ProviderConfig(id='provider_fixture',name='本地受控验收模型',base_url='http://127.0.0.1:19191/v1',model='controlled-fixture',enabled=True))
 db.add(m.Course(id='course_fixture',title='合成课程：学习计划验收',term='2026秋'))
 db.commit()
"""
log=(WORK/'services.log').open('w',encoding='utf-8')
processes=[]
try:
    subprocess.run([str(python),'-c',code],cwd=ROOT/'apps/api',env=env,check=True,stdout=log,stderr=log)
    processes.append(subprocess.Popen([str(python),'-m','uvicorn','app.main:app','--host','127.0.0.1','--port','18080'],cwd=ROOT/'apps/api',env=env,stdout=log,stderr=log))
    processes.append(subprocess.Popen(['node',str(ROOT/'apps/web/node_modules/vite/bin/vite.js'),'--configLoader','native','--port','15173','--strictPort'],cwd=ROOT/'apps/web',env=env,stdout=log,stderr=log))
    print('SYNTHETIC_TESTBENCH='+str(WORK),flush=True)
    print('URL=http://127.0.0.1:15173/chat',flush=True)
    print('STOP_FILE='+str(STOP_FILE),flush=True)
    deadline = (time.monotonic() + _args.ttl_seconds) if _args.ttl_seconds else None
    if deadline:
        print('TTL_SECONDS='+str(_args.ttl_seconds),flush=True)
    while not STOP_FILE.exists():
        if deadline is not None and time.monotonic() >= deadline:
            print('TTL_EXPIRED='+str(_args.ttl_seconds)+'; stopping to free 15173/18080/19191',flush=True)
            break
        if any(p.poll() is not None for p in processes): raise RuntimeError('Testbench service exited; inspect isolated services.log')
        time.sleep(.5)
finally:
    for process in reversed(processes):
        if process.poll() is None:
            process.terminate()
            try:process.wait(timeout=15)
            except subprocess.TimeoutExpired:process.kill();process.wait()
    server.shutdown();server.server_close();log.close()

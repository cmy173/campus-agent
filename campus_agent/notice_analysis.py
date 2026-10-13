"""Application service for notice analysis: offline rules plus an optional model extractor.

The provider call never runs inside a database transaction. Before a result is written the
account, the document, its content version and the run identity are re-checked, so a late
response cannot revive deleted data or land on another account.
"""
from datetime import datetime
from uuid import uuid4
import json
import threading

from . import notice_contract, notice_model
from .core import TZ
from .notice_contract import ACTIONABLE_KINDS, KIND_LABELS, PROMPT_VERSION, ContractError
from .notice_model import ModelFailure
from .services import extract_tasks

RULES = 'rules'
MODEL = 'model'
# Bound on candidate items produced by one analysis; events and times are already capped.
MAX_TASKS_PER_ANALYSIS = 40
RULES_NOTE = ('离线规则提取：按句子与关键词生成候选事项，不归并同一事件、不识别地点与适用对象。'
              '启用模型整理可得到事件摘要、分场次时间与地点。')


class AnalysisError(Exception):
    """Raised for caller-visible refusals; status maps directly to an HTTP status code."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


def now():
    return datetime.now(TZ).isoformat()


def analysis_key(body_sha, extractor, prompt_version, model):
    return '|'.join((body_sha, extractor, prompt_version, model))


def run_key(key):
    """analysis_key for a run tuple of (document, body_sha, extractor, prompt_version, model)."""
    return analysis_key(*key[1:])


def attachment_list(links):
    """Structured attachments with stable refs. URLs stay server-owned metadata."""
    return [{'ref': notice_contract.attachment_ref(link['url']), 'name': link['name'],
             'url': link['url'], 'parsed': False} for link in links]


def _due(value):
    try:
        return datetime.fromisoformat(value).astimezone(TZ).isoformat()
    except (ValueError, TypeError, AttributeError):
        return None


def _task(chunk_id, title, key, **fields):
    defaults = {'chunk': chunk_id, 'title': title[:300], 'due': None, 'raw_date': None,
                'origin': RULES, 'event_ref': '', 'analysis_key': key, 'evidence': [],
                'time_kind': '', 'time_precision': '', 'location': '', 'needs_attachment': 0,
                'attachment_refs': [], 'derived_note': ''}
    return defaults | fields


def rules_result(chunks, attachments, body_sha):
    """Offline candidate items. Reuses extract_tasks so rule behaviour stays single-sourced."""
    key = analysis_key(body_sha, RULES, PROMPT_VERSION, '')
    tasks, questions = [], []
    for chunk in chunks:
        for item in extract_tasks(chunk['text']):
            quote = item['title'][:300]
            evidence = [{'ref': chunk['ref'], 'quote': quote, 'chunk': chunk['id'],
                         'locator': chunk['locator']}]
            tasks.append(_task(chunk['id'], item['title'], key, due=item['due'],
                               raw_date=item['raw_date'], evidence=evidence))
            if not item['due']:
                questions.append(f'「{quote[:40]}」未识别到含年份和时刻的完整时间，请核对原文')
    for attachment in attachments:
        questions.append(f"附件《{attachment['name'][:60]}》未下载、未解析，可能包含时间、地点或名单")
    payload = {'notice_summary': '', 'coverage': 'partial', 'coverage_note': RULES_NOTE,
               'events': [], 'open_questions': questions[:notice_contract.MAX_QUESTIONS]}
    return payload, tasks


def tasks_from_payload(payload, chunks, attachments, key):
    """One candidate item per actionable time; deadlines are never merged into one due."""
    unparsed = any(not attachment.get('parsed') for attachment in attachments)
    tasks = []
    for event in payload.get('events', []):
        evidence = [{'ref': item['ref'], 'quote': item['quote'], 'chunk': chunks[item['ref']]['id'],
                     'locator': chunks[item['ref']]['locator']}
                    for item in event.get('evidence', []) if item['ref'] in chunks]
        if not evidence:
            continue
        location = '' if event.get('location_needs_attachment') else event.get('location', '')
        shared = {'origin': MODEL, 'event_ref': event.get('ref', ''), 'evidence': evidence,
                  'location': location, 'attachment_refs': list(event.get('attachment_refs', []))}
        actionable = [item for item in event.get('times', []) if item['kind'] in ACTIONABLE_KINDS]
        for item in actionable:
            due = _due(item['value']) if item['precision'] == 'datetime' else None
            label = KIND_LABELS[item['kind']]
            title = f"{event['title']}｜{label}" + ('' if due else f"：{item['raw']}")
            tasks.append(_task(evidence[0]['chunk'], title, key, due=due, raw_date=item['raw'],
                time_kind=item['kind'], time_precision=item['precision'], **shared,
                needs_attachment=1 if (event.get('location_needs_attachment') or
                                       (due is None and unparsed)) else 0,
                derived_note=item['derivation'] if item['derived'] else ''))
        if not actionable and event.get('actions'):
            tasks.append(_task(evidence[0]['chunk'], f"{event['title']}｜{event['actions'][0]}",
                               key, needs_attachment=1 if unparsed else 0, **shared))
    return tasks[:MAX_TASKS_PER_ANALYSIS]


def insert_tasks(db, doc_id, key, tasks):
    """Merge candidates by identity so a retry never duplicates or overwrites a decision.

    Identity is (event_ref, time_kind, title). A candidate the user reviewed, completed,
    ignored or edited is left untouched; unreviewed candidates are refreshed in place, and
    unreviewed candidates the new result no longer supports are removed.
    """
    rows = [dict(row) for row in db.execute('''SELECT id,event_ref,time_kind,title,reviewed,state
        FROM tasks WHERE document=? AND analysis_key=?''', (doc_id, key))]
    existing, decided = {}, set()
    for row in rows:
        identity = (row['event_ref'], row['time_kind'], row['title'])
        existing[identity] = row['id']
        if row['reviewed'] or row['state'] != 'pending':
            decided.add(identity)
    fresh = set()
    for task in tasks:
        identity = (task['event_ref'], task['time_kind'], task['title'][:300])
        fresh.add(identity)
        if identity in decided:
            continue
        if identity in existing:
            db.execute('''UPDATE tasks SET chunk=?,due=?,raw_date=?,origin=?,evidence=?,time_precision=?,
                location=?,needs_attachment=?,attachment_refs=?,derived_note=? WHERE id=?''',
                (task['chunk'], task['due'], task['raw_date'], task['origin'],
                 json.dumps(task['evidence'], ensure_ascii=False), task['time_precision'],
                 task['location'], task['needs_attachment'],
                 json.dumps(task['attachment_refs'], ensure_ascii=False), task['derived_note'],
                 existing[identity]))
            continue
        db.execute('''INSERT INTO tasks(id,document,chunk,title,due,raw_date,origin,event_ref,
            analysis_key,evidence,time_kind,time_precision,location,needs_attachment,
            attachment_refs,derived_note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (uuid4().hex, doc_id, task['chunk'], task['title'], task['due'], task['raw_date'],
             task['origin'], task['event_ref'], task['analysis_key'],
             json.dumps(task['evidence'], ensure_ascii=False), task['time_kind'],
             task['time_precision'], task['location'], task['needs_attachment'],
             json.dumps(task['attachment_refs'], ensure_ascii=False), task['derived_note']))
    for identity, task_id in existing.items():
        if identity not in fresh and identity not in decided:
            db.execute('DELETE FROM tasks WHERE id=?', (task_id,))


def save_source(db, owner, doc_id, platform, record, created):
    db.execute('''INSERT INTO document_sources(document,owner,platform,record_id,origin,college,
        title,published_at,body_sha256,attachments,has_unread_content,created)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(document) DO UPDATE SET platform=excluded.platform, record_id=excluded.record_id,
        origin=excluded.origin, college=excluded.college, title=excluded.title,
        published_at=excluded.published_at, body_sha256=excluded.body_sha256,
        attachments=excluded.attachments, has_unread_content=excluded.has_unread_content''',
        (doc_id, owner, platform, record.id, record.source, record.college, record.title,
         record.published_at, record.body_sha256,
         json.dumps(attachment_list([{'name': link.name, 'url': link.url}
                                     for link in record.attachments]), ensure_ascii=False),
         int(record.has_unread_content), created))


def _source_view(row):
    if not row:
        return None
    return {'platform': row['platform'], 'record_id': row['record_id'], 'origin': row['origin'],
            'college': row['college'], 'title': row['title'], 'published_at': row['published_at'],
            'body_sha256': row['body_sha256'], 'has_unread_content': bool(row['has_unread_content']),
            'attachments': json.loads(row['attachments'])}


def _error_view(raw):
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {'code': 'UNKNOWN', 'message': str(raw)}
    except json.JSONDecodeError:
        return {'code': 'UNKNOWN', 'message': raw}


def _analysis_view(row):
    try:
        payload = json.loads(row['payload'])
    except json.JSONDecodeError:
        payload = {}
    return {'extractor': row['extractor'], 'model': row['model'],
            'prompt_version': row['prompt_version'], 'status': row['status'],
            'error': _error_view(row['error']), 'result_at': row['result_at'],
            'attempts': row['attempts'], 'usage': json.loads(row['usage'] or '{}'),
            'body_sha256': row['body_sha256'], 'payload': payload}


class NoticeAnalysisService:
    def __init__(self, store, submit=None, extractor_factory=None):
        self.store = store
        self.submit = submit
        self.extractor_factory = extractor_factory or notice_model.extractor
        self.lock = threading.Lock()
        self.inflight = set()

    # ---------------------------------------------------------------- reading

    def read(self, owner, doc_id):
        with self.store.connect() as db:
            doc = db.execute('SELECT id,name,kind,status,sha,created FROM documents WHERE id=? AND owner=?',
                             (doc_id, owner)).fetchone()
            if not doc:
                return None
            source = db.execute('SELECT * FROM document_sources WHERE document=? AND owner=?',
                                (doc_id, owner)).fetchone()
            rows = db.execute('SELECT * FROM notice_analyses WHERE document=? AND owner=? ORDER BY updated DESC',
                              (doc_id, owner)).fetchall()
        return self._view(doc, source, rows)

    def list_all(self, owner, limit=50):
        with self.store.connect() as db:
            docs = db.execute('''SELECT id,name,kind,status,sha,created FROM documents
                WHERE owner=? AND kind='notice' ORDER BY created DESC LIMIT ?''', (owner, limit)).fetchall()
            result = []
            for doc in docs:
                source = db.execute('SELECT * FROM document_sources WHERE document=? AND owner=?',
                                    (doc['id'], owner)).fetchone()
                rows = db.execute('SELECT * FROM notice_analyses WHERE document=? AND owner=? ORDER BY updated DESC',
                                  (doc['id'], owner)).fetchall()
                result.append(self._view(doc, source, rows))
        return result

    @staticmethod
    def _view(doc, source, rows):
        return {'document': doc['id'], 'name': doc['name'], 'status': doc['status'],
                'created': doc['created'], 'body_sha256': doc['sha'],
                'source': _source_view(source), 'analyses': [_analysis_view(row) for row in rows]}

    # --------------------------------------------------------------- analysis

    def request(self, owner, doc_id, mode, consent=False):
        doc, source, chunks = self._context(owner, doc_id)
        attachments = json.loads(source['attachments']) if source else []
        if mode == 'rules':
            with self.store.connect() as db:
                if not self._ready(db, owner, doc_id, doc['sha']):
                    return self.read(owner, doc_id)
                self.write_rules(db, owner, doc_id, doc['sha'], chunks, attachments)
            return self.read(owner, doc_id)
        if mode != MODEL:
            raise AnalysisError(400, '未知整理方式')
        config = notice_model.configuration()
        if not config['configured']:
            raise AnalysisError(409, config['error'] or notice_model.MESSAGES['NOT_CONFIGURED'])
        if consent is not True:
            raise AnalysisError(400, '启用云端整理前需要明确同意把通知内容发送给所选提供方')
        key = (doc_id, doc['sha'], config['provider'], PROMPT_VERSION, config['model'])
        with self.lock:
            if key in self.inflight:
                return self.read(owner, doc_id)
            self.inflight.add(key)
        run_id = uuid4().hex
        try:
            self._mark_running(owner, key, run_id)
            if self.submit is None:
                self._run_model(owner, key, run_id)
            else:
                self.submit(self._run_model, owner, key, run_id)
        except BaseException:
            with self.lock:
                self.inflight.discard(key)
            raise
        return self.read(owner, doc_id)

    def _context(self, owner, doc_id):
        with self.store.connect() as db:
            doc = db.execute('SELECT id,name,kind,sha,status FROM documents WHERE id=? AND owner=?',
                             (doc_id, owner)).fetchone()
            if not doc:
                raise AnalysisError(404, '资料不存在')
            if doc['status'] != 'ready':
                raise AnalysisError(409, '资料尚未解析完成，请稍后重试')
            if doc['kind'] != 'notice':
                raise AnalysisError(400, '仅“通知”类资料支持事件整理')
            source = db.execute('SELECT * FROM document_sources WHERE document=? AND owner=?',
                                (doc_id, owner)).fetchone()
            chunks = self._chunks(db, doc_id)
        if not chunks:
            raise AnalysisError(409, '资料没有可分析的段落')
        return dict(doc), (dict(source) if source else None), chunks

    @staticmethod
    def _chunks(db, doc_id):
        rows = db.execute('SELECT id,position,locator,text FROM chunks WHERE document=? ORDER BY position',
                          (doc_id,)).fetchall()
        return [{'id': row['id'], 'ref': notice_contract.paragraph_ref(row['position']),
                 'locator': row['locator'], 'text': row['text']} for row in rows]

    @staticmethod
    def _ready(db, owner, doc_id, body_sha):
        """The document must still exist, belong to this account and keep the analysed version."""
        row = db.execute('SELECT id,name,sha,status FROM documents WHERE id=? AND owner=?',
                         (doc_id, owner)).fetchone()
        return row if row and row['sha'] == body_sha and row['status'] == 'ready' else None

    def write_rules(self, db, owner, doc_id, body_sha, chunks, attachments):
        payload, tasks = rules_result(chunks, attachments, body_sha)
        self._upsert(db, owner, (doc_id, body_sha, RULES, PROMPT_VERSION, ''), 'ready',
                     payload | {'extractor': RULES, 'model': '', 'prompt_version': PROMPT_VERSION},
                     tasks, {})

    def _mark_running(self, owner, key, run_id):
        with self.store.connect() as db:
            # A failed or superseded attempt keeps the previous payload and result_at.
            db.execute('''INSERT INTO notice_analyses(document,owner,body_sha256,extractor,
                prompt_version,model,run_id,status,payload,attempts,updated) VALUES(?,?,?,?,?,?,?,'running','{}',1,?)
                ON CONFLICT(document,body_sha256,extractor,prompt_version,model) DO UPDATE SET
                run_id=excluded.run_id, status='running', error=NULL, updated=excluded.updated,
                attempts=notice_analyses.attempts+1''',
                (key[0], owner, key[1], key[2], key[3], key[4], run_id, now()))

    def _run_model(self, owner, key, run_id):
        doc_id, body_sha, extractor, prompt_version, model = key
        try:
            with self.store.connect() as db:
                doc = self._ready(db, owner, doc_id, body_sha)
                if not doc:
                    return 'discarded'
                source = db.execute('SELECT * FROM document_sources WHERE document=? AND owner=?',
                                    (doc_id, owner)).fetchone()
                chunks = self._chunks(db, doc_id)
            provenance = _source_view(source)
            attachments = (provenance or {}).get('attachments', [])
            model_input = notice_contract.build_input(
                (provenance or {}).get('title') or doc['name'], provenance, chunks)
            # The transaction above is closed: no lock is held across the provider call.
            result = self.extractor_factory(owner).extract(model_input)
            paragraphs = {chunk['ref']: chunk['text'] for chunk in chunks}
            payload, corrected = notice_contract.validate(
                result['content'], paragraphs, {item['ref'] for item in attachments})
            if model_input['coverage'] == 'partial':
                # Server-authoritative: a truncated input can never be reported as fully analysed.
                payload['coverage'] = 'partial'
                payload['coverage_note'] = model_input['coverage_note'] + (
                    '；' + payload['coverage_note'] if payload['coverage_note'] else '')
            payload |= {'extractor': extractor, 'model': model, 'prompt_version': prompt_version,
                        'evidence_corrected': corrected, 'input_coverage': model_input['coverage']}
            return self._commit(owner, key, run_id, payload, result['usage'])
        except ModelFailure as exc:
            return self._fail(owner, key, run_id, exc.code, exc.message)
        except ContractError as exc:
            return self._fail(owner, key, run_id, 'CONTRACT_' + exc.code, exc.message)
        except Exception:
            # Provider or storage surprises are reported without their text.
            return self._fail(owner, key, run_id, 'INTERNAL', '整理失败，请重试')
        finally:
            with self.lock:
                self.inflight.discard(key)

    def _commit(self, owner, key, run_id, payload, usage):
        doc_id, body_sha = key[0], key[1]
        with self.store.connect() as db:
            if not self._ready(db, owner, doc_id, body_sha):
                return 'discarded'
            row = db.execute('''SELECT run_id,owner FROM notice_analyses WHERE document=? AND
                body_sha256=? AND extractor=? AND prompt_version=? AND model=?''', key).fetchone()
            if not row or row['run_id'] != run_id or row['owner'] != owner:
                return 'discarded'
            chunks = {chunk['ref']: chunk for chunk in self._chunks(db, doc_id)}
            source = db.execute('SELECT attachments FROM document_sources WHERE document=? AND owner=?',
                                (doc_id, owner)).fetchone()
            attachments = json.loads(source['attachments']) if source else []
            tasks = tasks_from_payload(payload, chunks, attachments, run_key(key))
            self._upsert(db, owner, key, 'partial' if payload['coverage'] == 'partial' else 'ready',
                         payload, tasks, usage)
        return 'ok'

    @staticmethod
    def _upsert(db, owner, key, status, payload, tasks, usage):
        stamp = now()
        db.execute('''INSERT INTO notice_analyses(document,owner,body_sha256,extractor,prompt_version,
            model,run_id,status,payload,result_at,error,attempts,usage,updated)
            VALUES(?,?,?,?,?,?,'',?,?,?,NULL,1,?,?)
            ON CONFLICT(document,body_sha256,extractor,prompt_version,model) DO UPDATE SET
            status=excluded.status, payload=excluded.payload, result_at=excluded.result_at,
            error=NULL, usage=excluded.usage, updated=excluded.updated''',
            (key[0], owner, key[1], key[2], key[3], key[4], status,
             json.dumps(payload, ensure_ascii=False), stamp, json.dumps(usage, ensure_ascii=False), stamp))
        insert_tasks(db, key[0], run_key(key), tasks)

    def _fail(self, owner, key, run_id, code, message):
        with self.store.connect() as db:
            row = db.execute('''SELECT run_id,owner FROM notice_analyses WHERE document=? AND
                body_sha256=? AND extractor=? AND prompt_version=? AND model=?''', key).fetchone()
            if not row or row['run_id'] != run_id or row['owner'] != owner:
                return 'discarded'
            db.execute('''UPDATE notice_analyses SET status='failed', error=?, updated=?
                WHERE document=? AND body_sha256=? AND extractor=? AND prompt_version=? AND model=?''',
                (json.dumps({'code': code, 'message': message}, ensure_ascii=False), now(), *key))
        return 'failed'

import pytest
from test_textbook_library import library
from app import worker, secrets_store, providers
from app.models import Recording, ProviderConfig, SourceRef, Job, OWNER_RECORDING, utc_now

@pytest.fixture
def classroom(library, monkeypatch):
    client, sessions = library
    monkeypatch.setattr(secrets_store, 'get_key', lambda _: 'test-secret')
    with sessions() as db:
        db.add(Recording(id='rec_class', course_id='crs_textbook', title='第一讲', transcript_status='ready'))
        db.add(Recording(id='rec_unbound', course_id='crs_textbook', title='非智云场次'))
        db.add(SourceRef(owner_type=OWNER_RECORDING, owner_id='rec_class', platform='zykt', external_id='123', seq=0, observed_at=utc_now()))
        db.add(ProviderConfig(id='provider_class', name='测试', base_url='https://example.org/v1', model='test', timeout_seconds=60))
        db.commit()
    return client, sessions

def request(client, **values):
    return client.post('/api/v1/recordings/rec_class/summary', json={'provider_id': 'provider_class', 'transcript': '[00:00] 原子结构', **values})

def test_validates_scene_and_empty_text(classroom):
    client, _ = classroom
    assert [row['id'] for row in client.get('/api/v1/classroom/recordings').json()['items']] == ['rec_class']
    assert request(client, transcript='  ').status_code == 400
    assert request(client, detail='invalid').status_code == 400
    assert client.post('/api/v1/recordings/rec_unbound/summary', json={'provider_id':'provider_class','transcript':'text'}).status_code == 400

def test_long_class_keeps_last_segment_and_saves_history(classroom, monkeypatch):
    client, sessions = classroom
    calls = []
    def mock(*args, **kwargs):
        calls.append((args[4], kwargs['max_tokens']))
        return '本段知识总结', 200
    monkeypatch.setattr(providers, 'call_chat_completions', mock)
    response = request(client, detail='detailed', transcript='前段知识' * 6000 + '课程结尾特有知识')
    assert response.status_code == 202
    job_id = response.json()['job']['id']
    assert request(client, detail='detailed', transcript='前段知识' * 6000 + '课程结尾特有知识').json()['job']['id'] == job_id
    with sessions() as db:
        worker.process_job(db, db.get(Job, job_id))
    history = client.get('/api/v1/classroom/summaries').json()['items'][0]
    assert history['summary'] == '本段知识总结'
    assert history['job']['status'] == 'succeeded'
    assert any('课程结尾特有知识' in messages[1]['content'] for messages, _ in calls)
    assert len(calls) == 4
    assert all(tokens == 65536 for _, tokens in calls)
    assert 'test-secret' not in str(history)

def test_provider_failure_never_saves_fake_summary(classroom, monkeypatch):
    client, sessions = classroom
    def mock(*args, **kwargs):
        raise providers.ProviderCallError('PROVIDER_TIMEOUT', '服务超时', retryable=True)
    monkeypatch.setattr(providers, 'call_chat_completions', mock)
    job_id = request(client).json()['job']['id']
    with sessions() as db:
        worker.process_job(db, db.get(Job, job_id))
    item = client.get('/api/v1/classroom/summaries').json()['items'][0]
    assert item['summary'] is None
    assert item['job']['errors'][0]['code'] == 'PROVIDER_TIMEOUT'

def test_cancelled_job_does_not_call_provider(classroom, monkeypatch):
    client, sessions = classroom
    monkeypatch.setattr(providers, 'call_chat_completions', lambda *a, **k: pytest.fail('cancelled request called API'))
    job_id = request(client).json()['job']['id']
    with sessions() as db:
        job = db.get(Job, job_id); job.cancel_requested = True; db.commit()
        worker.process_job(db, job)
        assert job.status == 'cancelled'


def test_brief_mode_compresses_oversized_draft(classroom, monkeypatch):
    client, sessions = classroom
    outputs = iter(['过长总结' * 700, '简略版：核心结论与待复习事项'])
    instructions = []
    def mock(*args, **kwargs):
        instructions.append(args[4][1]['content'])
        return next(outputs), 200
    monkeypatch.setattr(providers, 'call_chat_completions', mock)
    job_id = request(client).json()['job']['id']
    with sessions() as db:
        worker.process_job(db, db.get(Job, job_id))
    item = client.get('/api/v1/classroom/summaries').json()['items'][0]
    assert item['summary'] == '简略版：核心结论与待复习事项'
    assert '500 至 900 字' in instructions[-1]

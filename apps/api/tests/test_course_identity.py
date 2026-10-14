import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from app.db import Base
from app.models import Course, SourceRef, Resource, Job, Connection, utc_now
from app.course_identity import matching_course, repair_duplicate_courses, enrich_course
from app.providers import normalize_base_url, run_connection_test
from app.errors import ApiError


@pytest.fixture
def db():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def add_course(db, id, platform, teacher=None, term=None):
    course = Course(id=id, title='光学', teacher=teacher, term=term)
    db.add(course)
    db.add(SourceRef(owner_type='course', owner_id=id, platform=platform, external_id=id, observed_at=utc_now()))
    db.flush()
    return course


def test_cross_platform_identity_alias_terms_and_sparse_metadata(db):
    course = add_course(db, 'x', 'xzzd', '王老师', '2026-2027秋冬')
    assert matching_course(db, 'zykt', '光学', '2026-20271', '王老师') is course
    assert matching_course(db, 'zykt', '光学', None, None) is course
    assert matching_course(db, 'zykt', '光学', '2025-20261', '王老师') is None
    assert matching_course(db, 'zykt', '光学', None, '李老师') is None
    assert matching_course(db, 'xzzd', '光学', None, None) is None


def test_ambiguous_same_title_does_not_merge(db):
    add_course(db, 'x1', 'xzzd', '王老师')
    add_course(db, 'x2', 'xzzd', '李老师')
    assert matching_course(db, 'zykt', '光学', None, None) is None


def test_repair_preserves_material_ids_and_old_course_record(db):
    target = add_course(db, 'x', 'xzzd', '王老师', '2026-2027秋冬')
    duplicate = add_course(db, 'z', 'zykt')
    db.add(Resource(id='res', course_id='z', kind='document', title='讲义', version=1, parse_status='ready'))
    db.flush()
    assert repair_duplicate_courses(db) == [('z', 'x')]
    assert db.get(Resource, 'res').course_id == 'x'
    assert duplicate.archived_at is not None
    assert db.execute(select(SourceRef).where(SourceRef.platform == 'zykt')).scalar_one().owner_id == target.id
    assert repair_duplicate_courses(db) == []
    enrich_course(target, None, None)
    assert target.teacher == '王老师'


def test_active_jobs_defer_repair(db):
    add_course(db, 'x', 'xzzd')
    add_course(db, 'z', 'zykt')
    db.add(Job(id='job', type='sync', status='queued'))
    db.flush()
    assert repair_duplicate_courses(db) == []


def test_import_links_second_platform_and_reimport_keeps_teacher(db):
    from app.routers.connections import import_platform_courses
    from app.schemas import ImportPlatformCoursesRequest
    add_course(db, 'x', 'xzzd', '王老师', '2026-2027秋冬')
    db.add(Connection(id='conn_z', platform='zykt', display_name='智云', status='ok'))
    db.flush()
    body = ImportPlatformCoursesRequest(courses=[{'external_id': 'platform-z', 'title': '光学'}])
    for _ in range(2):
        result = import_platform_courses('conn_z', body, db)
        assert result['items'][0]['id'] == 'x'
        assert result['items'][0]['teacher'] == '王老师'
        assert len(result['items'][0]['source_refs']) == 2
    assert len(db.execute(select(Course)).scalars().all()) == 1


def test_deepseek_console_address_is_rejected_before_sending_key():
    with pytest.raises(ApiError, match='api.deepseek.com'):
        normalize_base_url('https://platform.deepseek.com/v1')
    result = run_connection_test('https://platform.deepseek.com/v1', 'deepseek', 'fake-test-key', 120)
    assert not result['ok'] and 'api.deepseek.com' in result['error']['message']
    result = run_connection_test('https://api.deepseek.com/v1', 'deepseek', 'fake-test-key', 120)
    assert not result['ok'] and '模型标识' in result['error']['message']

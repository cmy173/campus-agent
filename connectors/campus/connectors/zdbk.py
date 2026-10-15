"""Capabilities around the existing, tested protocols. Sessions are injected."""
from ..campus_endpoints import TIMETABLE, GRADES
from ..connector_contracts import ConnectorSpec, FetchResult
from ..campus_common import CampusError


class ZdbkAdapter:
    spec = ConnectorSpec('zdbk', ('courses', 'grades'), 'cas+zdbk', pagination=True)

    def fetch(self, capability, query, session):
        if capability == 'grades':
            rows, complete, total = session.grades()
            return FetchResult([row | {'source': GRADES.split('?')[0]} for row in rows],
                'complete' if complete else 'partial',
                [] if complete else [{'code': 'PARTIAL', 'message': '成绩分页尚未完成'}])
        if query.year is None:
            raise CampusError('UNSUPPORTED', '显式同步课程需要单个学期范围；旧页面保留全学期批次同步')
        term = next((t for t in session.terms() if t['year'] == query.year and t['semester'] == query.semester), None)
        if term is None:
            raise CampusError('UNSUPPORTED', '平台未声明可查询的学期范围')
        profile, rows = session.timetable(term.get('query_year', query.year), query.semester)
        expected = getattr(session, 'expected_identity', '')
        if expected and profile and profile.get('student_id') and profile['student_id'] != expected:
            raise CampusError('AUTH_EXPIRED', '教务响应身份发生变化')
        warnings = getattr(session, 'timetable_warnings', [])
        return FetchResult([row | {'year': query.year, 'semester': query.semester,
            'term': term['term'], 'source': TIMETABLE} for row in rows],
            'partial' if warnings else 'complete',
            [{'code': 'PARTIAL', 'message': '部分课程条目未能解析'}] if warnings else [])

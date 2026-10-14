"""Academic views with explicit live/cache/default provenance; GPA uses source values."""
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import re

from .core import TZ
from .academic_defaults import semester_seasons


def number(value):
    try:
        text = str(value).strip()
        if len(text) > 16 or not re.fullmatch(r'\d+(?:\.\d+)?', text):
            return None
        result = Decimal(text)
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def grade_summary(section):
    rows = (section or {}).get('data', [])
    def summarize(items):
        eligible = [(number(r.get('credits')), number(r.get('gpa'))) for r in items]
        eligible = [(c, g) for c, g in eligible if c is not None and c > 0 and g is not None]
        credits = sum((c for c, _ in eligible), Decimal(0))
        mean = sum((c*g for c, g in eligible), Decimal(0)) / credits if credits else None
        return {'gpa': float(mean.quantize(Decimal('.01'))) if mean is not None else None,
                'credits': float(credits), 'included': len(eligible), 'excluded': len(items)-len(eligible),
                'records': len(items)}
    terms = sorted({r.get('term') or '未提供学期' for r in rows}, reverse=True)
    return {'overall': summarize(rows), 'terms': [{'term': t, **summarize([
        r for r in rows if (r.get('term') or '未提供学期') == t])} for t in terms],
        'complete': (section or {}).get('complete', False), 'stale': (section or {}).get('stale', False),
        'fetched_at': (section or {}).get('fetched_at'),
        'method': 'Σ(教务返回绩点 × 正学分) / Σ正学分；缺失绩点或非正学分不参与。未换算分制、筛选主修或替换重修记录，不等同于官方认定均绩。'}


def course_periods(row):
    start, duration = number(row.get('start_period')), number(row.get('duration'))
    if start is not None and duration is not None and start == int(start) and duration == int(duration):
        first, last = int(start), int(start+duration-1)
    else:
        match = re.fullmatch(r'\s*(\d{1,2})(?:\s*[-–]\s*(\d{1,2}))?\s*(?:节)?\s*', row.get('periods', ''))
        if not match:
            return None
        first, last = int(match[1]), int(match[2] or match[1])
    return (first, last) if 1 <= first <= last <= 20 else None


def week_matches(row, week):
    """None means unsupported syntax; never silently turn unknown weeks into all weeks."""
    raw = (row.get('weeks') or '').strip()
    parity = row.get('odd_even') or ''
    if not raw and not parity:
        return None
    if parity not in ('', '0', '1', '2'):
        return None
    if raw:
        expression = re.sub(r'\s', '', raw).replace('，', ',').replace('、', ',').replace('–', '-')
        expression = expression.replace('周', '')
        marker = re.search(r'\(([单双])\)$', expression)
        if marker:
            if parity and parity != '2' and parity != ('0' if marker[1] == '单' else '1'):
                return None
            parity = '0' if marker[1] == '单' else '1'
            expression = expression[:marker.start()]
        if not re.fullmatch(r'\d{1,2}(?:-\d{1,2})?(?:,\d{1,2}(?:-\d{1,2})?)*', expression):
            return None
        allowed = set()
        for token in expression.split(','):
            ends = [int(x) for x in token.split('-')]
            first, last = ends[0], ends[-1]
            if not 1 <= first <= last <= 53:
                return None
            allowed.update(range(first, last+1))
        if week not in allowed:
            return False
    return not ((parity == '0' and week % 2 == 0) or (parity == '1' and week % 2 == 1))


def agenda(section, calendars, selected_day=None, term_key=None):
    today = datetime.now(TZ).date()
    day = date.fromisoformat(selected_day) if selected_day else today
    terms = (section or {}).get('terms', [])
    if not terms and section and section.get('year'):
        terms = [section | {'term': section['year']+' · '+section['semester']}]
    term = next((t for t in terms if t['year']+'/'+t['semester'] == term_key), None) if term_key else None
    if not term_key:
        # Calendar coverage, then the source's current selection; never newest year = current.
        def covers(t):
            return any(s['start'] <= day.isoformat() <= s['end'] and
                       (not semester_seasons(t['semester']) or s['name'] in semester_seasons(t['semester']))
                       for s in (calendars.get(t['year']+'/'+t['semester']) or {}).get('seasons', []))
        term = next((t for t in terms if t.get('current') and not t.get('stale') and covers(t)), None)
        term = term or next((t for t in terms if not t.get('stale') and covers(t)), None)
        term = term or next((t for t in terms if covers(t)), None)
        term = term or next((t for t in terms if t.get('current')), None)
    monday = day - timedelta(days=day.weekday())
    result = {'today': today.isoformat(), 'day': day.isoformat(), 'week_start': monday.isoformat(),
              'term_key': term['year']+'/'+term['semester'] if term else '',
              'term': term.get('term') if term else None, 'events': [], 'unplaced': [],
              'calendar': None, 'dated': False, 'stale': bool(term and (term.get('stale') or any(r.get('stale') for r in term['data']))),
              'fetched_at': term.get('fetched_at') if term else None}
    if not term:
        result['message'] = '尚未确认当前学期。请先同步，或选择要查看的学期。'
        return result
    config = calendars.get(result['term_key'])
    result['calendar'] = config
    result['dated'] = bool(config and (config.get('verified') or config.get('usable')))
    result['source_status'] = (config or {}).get('source_status', 'cache' if config and config.get('stale') else 'live')
    if not result['dated']:
        result['message'] = '校历尚未完成程序核对，展示星期与节次，不推算今日课程。'
        return result
    unknown = set()
    for offset in range(7):
        actual = monday + timedelta(days=offset)
        iso = actual.isoformat()
        uncertain = iso > config.get('valid_until', iso) or iso in config.get('uncertain_dates', [])
        if iso in config['holidays']:
            continue
        logical = date.fromisoformat(config['exchanges'].get(iso, iso))
        season = next((s for s in config['seasons'] if s['start'] <= logical.isoformat() <= s['end']), None)
        if not season:
            continue
        week = (logical-date.fromisoformat(season['start'])).days//7+1
        for index, row in enumerate(term['data']):
            raw_season = row.get('season') or ''
            season_text = ''.join(c for c in '秋冬春夏' if c in raw_season)
            assumptions = []
            if not raw_season:
                season_text = semester_seasons(term['semester']) or ''.join(
                    c for c in '秋冬春夏' if c in term.get('term', '')) or ''.join(s['name'] for s in config['seasons'])
                assumptions.append('季学期缺失，按所查询学期展示')
            periods = course_periods(row)
            weekday = row.get('weekday', '')
            if season_text and all(c in '秋冬春夏短' for c in season_text) and season['name'] not in season_text:
                continue
            matches = week_matches(row, week)
            if not row.get('weeks') and not row.get('odd_even'):
                unknown.add(index)
                continue  # Missing weeks are not evidence of a weekly class.
            if (not season_text or any(c not in '秋冬春夏短' for c in season_text)
                    or not periods or not re.fullmatch(r'[1-7]', weekday) or matches is None
                    or periods[1] > len(config['period_times'])):
                unknown.add(index)
                continue
            if season['name'] not in season_text or int(weekday) != logical.isoweekday() or not matches:
                continue
            first, last = periods
            start, end = config['period_times'][first-1][0], config['period_times'][last-1][1]
            event = dict(row, date=iso, start=iso+'T'+start+':00+08:00', end=iso+'T'+end+':00+08:00',
                         time=start+'–'+end, first_period=first, last_period=last,
                         week=week, teaching_date=logical.isoformat(), fetched_at=row.get('fetched_at', term.get('fetched_at')),
                         provisional=bool(assumptions or uncertain or row.get('stale') or config.get('stale') or not config.get('verified')),
                         assumptions=assumptions, source_status=result['source_status'])
            # A moved day uses the original course's weekday/week/season, but its actual date.
            result['events'].append(event)
    for index, row in enumerate(term['data']):
        if index in unknown:
            result['unplaced'].append(row)
    result['events'].sort(key=lambda e: (e['start'], e['end'], e['name']))
    for index, event in enumerate(result['events']):
        event['conflict'] = any(other['date'] == event['date'] and other['start'] < event['end']
                                and event['start'] < other['end'] for j, other in enumerate(result['events']) if j != index)
    result['message'] = ('按默认日期与节次展示教务课程；日期为推算，临时停课、调课以学校通知为准。'
        if result['source_status'] == 'fallback' else '使用同学期校历缓存展示教务课程，保留原获取时间。'
        if result['source_status'] == 'cache' else '按教务课表与校历展示；教师或院系临时调课以最新通知为准。')
    result['uncertain_dates'] = [d.isoformat() for d in (monday+timedelta(days=i) for i in range(7))
                                 if d.isoformat() > config.get('valid_until', d.isoformat())
                                 or d.isoformat() in config.get('uncertain_dates', [])]
    return result

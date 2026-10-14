"""Bounded timetable probes and transparent calendar defaults inspired by Celechron."""
from datetime import date, timedelta
import re


SEMESTER_PATTERN = r'(?:[1-9]\d{0,2}|(?:0|[1-9]\d{0,2})\|\d{1,3}|[1-9]\d{0,2}\|(?:[\u4e00-\u9fff]|(?:[秋冬春夏]|秋冬|春夏)(?:季|学期|季学期)?))'
DEFAULT_TIMES = [['08:00','08:45'], ['08:50','09:35'], ['10:00','10:45'], ['10:50','11:35'],
    ['11:40','12:25'], ['13:25','14:10'], ['14:15','15:00'], ['15:05','15:50'],
    ['16:15','17:00'], ['17:05','17:50'], ['18:50','19:35'], ['19:40','20:25'],
    ['20:30','21:15'], ['21:20','22:05'], ['22:10','22:55']]


def academic_year(value):
    match = re.fullmatch(r'((?:19|20)\d{2})(?:[-—–]((?:19|20)\d{2}))?', str(value))
    if match and (not match[2] or int(match[2]) == int(match[1])+1):
        return match[1]
    return None


def semester_order(value):
    seasons = semester_seasons(value)
    return (max({'秋':1,'冬':2,'春':3,'夏':4}[s] for s in seasons) if seasons else int(value.split('|', 1)[0])), value


def semester_seasons(value):
    return ''.join(c for c in '秋冬春夏' if c in value.split('|', 1)[1]) if '|' in value else ''


def calendar_family(term):
    code = term['semester']
    label = term.get('term', '')
    if any(c in label for c in '秋冬') and not any(c in label for c in '春夏'):
        return '3'
    if any(c in label for c in '春夏') and not any(c in label for c in '秋冬'):
        return '12'
    seasons = semester_seasons(code)
    if seasons and all(s in '秋冬' for s in seasons):
        return '3'
    if seasons and all(s in '春夏' for s in seasons):
        return '12'
    if code in ('1', '3'):
        return '3'
    if code in ('2', '12'):
        return '12'
    return None


def default_calendar(term, template=None):
    family = calendar_family(term)
    if not family:
        return None
    year = int(term['year'])
    first = date(year, 9, 14) if family == '3' else date(year+1, 2, 20)
    first += timedelta(days=(-first.weekday()) % 7)
    names = ('秋', '冬') if family == '3' else ('春', '夏')
    seasons = [{'name': name, 'start': (first+timedelta(days=56*i)).isoformat(),
                'end': (first+timedelta(days=56*i+55)).isoformat()} for i, name in enumerate(names)]
    times = (template or {}).get('period_times') or DEFAULT_TIMES
    return {'verified': False, 'usable': True, 'stale': False, 'source_status': 'fallback',
        'seasons': seasons, 'period_times': times, 'holidays': [], 'exchanges': {},
        'uncertain_dates': [], 'valid_until': seasons[-1]['end'],
        'scope': '默认主校区本科时间；日期为推算，未补造节假日或调课',
        'method': '参照 Celechron：秋季从 9 月 14 日起的周一、春季从 2 月 20 日起的周一开始；每季八周。',
        'sources': []}


def resolve_calendar(term, calendars):
    old = calendars.get(term['year']+'/'+term['semester']) or {}
    if old.get('seasons') and old.get('period_times') and (old.get('verified') or old.get('usable')):
        return old | {'usable': True, 'source_status': 'cache' if old.get('stale') else old.get('source_status', 'live')}
    template = next((c for c in calendars.values() if c.get('verified') and c.get('period_times')), None)
    return default_calendar(term, template)


def fallback_terms(snapshot, today):
    """Query real endpoints; never manufacture courses or claim the range is complete."""
    current = today.year if today.month >= 9 else today.year-1
    saved = (snapshot.get('courses') or {}).get('terms', [])
    years = {current}
    for row in (snapshot.get('grades') or {}).get('data', []):
        match = re.match(r'((?:19|20)\d{2})[-—](?:19|20)\d{2}', row.get('term', ''))
        if match and current-10 <= int(match[1]) <= current:
            years.add(int(match[1]))
    years.update(range(min(years), current+1))
    profile = (snapshot.get('profile') or {}).get('data', {})
    enrollment = academic_year(profile.get('year', ''))
    if enrollment and current-10 <= int(enrollment) <= current:
        years.update(range(int(enrollment), current+1))
    if len(years) == 1 and not enrollment and not saved:
        years.update(range(current-7, current+1))
    terms = {(t['year'], t['semester']): {k: t[k] for k in
             ('year','semester','term','current','query_year','range_source') if k in t} for t in saved}
    season = '秋' if today.month in (9,10) else '冬' if today.month in (11,12,1) else '春' if today.month in (2,3,4) else '夏'
    for year in sorted(years, reverse=True):
        for code in ('1|秋','1|冬','2|春','2|夏'):
            terms.setdefault((str(year), code), {'year': str(year), 'query_year': f'{year}-{year+1}',
                'semester': code, 'term': f'{year}-{year+1} · {code[-1]}',
                'current': year == current and code[-1] == season, 'range_source': 'fallback'})
    return sorted(terms.values(), key=lambda t: (t.get('current', False), t['year'], semester_order(t['semester'])), reverse=True)[:180]


def merge_term_catalog(live, previous, saved):
    """Remember discovered semesters without querying ones the page no longer offers."""
    keys=('year','semester','term','current','query_year','range_source')
    catalog={(t['year'],t['semester']):{k:t[k] for k in keys if k in t}
             for t in list(saved)+list(previous)}
    live_keys={(t['year'],t['semester']) for t in live}
    for term in live:
        key=(term['year'],term['semester'])
        catalog[key]=catalog.get(key,{})|{k:term[k] for k in keys if k in term}
    current_keys={(t['year'],t['semester']) for t in live if t.get('current')}
    for key,term in catalog.items():
        term['available']=key in live_keys
        if current_keys:term['current']=key in current_keys
    return sorted(catalog.values(),key=lambda t:(t.get('current',False),t['year'],semester_order(t['semester'])),reverse=True)

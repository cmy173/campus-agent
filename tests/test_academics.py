"""Academic calculations and public-calendar validation, without student data."""
from datetime import datetime
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from connectors.campus.academics import agenda, grade_summary, course_periods, week_matches
from connectors.campus.calendar_sources import CalendarClient, quarters, period_times, teaching_changes, CATALOG, ENGLISH, NEWS
from connectors.campus.campus import CampusError


TIMES = [['08:00','08:45'],['08:50','09:35'],['10:00','10:45'],['10:50','11:35'],['11:40','12:25'],
         ['13:25','14:10'],['14:15','15:00'],['15:05','15:50'],['16:15','17:00'],['17:05','17:50'],
         ['18:50','19:35'],['19:40','20:25'],['20:30','21:15']]
CONFIG = {'verified': True, 'stale': False, 'period_times': TIMES, 'holidays': ['2026-10-05'],
          'exchanges': {'2026-10-10': '2026-10-07'}, 'uncertain_dates': ['2026-10-24'],
          'valid_until': '2026-12-30', 'seasons': [
              {'name': '秋', 'start': '2026-09-14', 'end': '2026-11-06'},
              {'name': '冬', 'start': '2026-11-09', 'end': '2027-01-05'}]}
ROW = {'name': '合成课程', 'weekday': '5', 'start_period': '1', 'duration': '2', 'season': '秋',
       'weeks': '1-8', 'odd_even': '2', 'location': '合成教室'}
COURSES = {'terms': [{'year': '2026', 'semester': '3', 'current': True, 'term': '2026-2027 · 秋冬',
                     'data': [ROW], 'fetched_at': '2026-10-01T10:00:00+08:00', 'stale': False}]}
ENGLISH_FIXTURE = '''<div class="item"><div class="title">Autumn Quarter 2026</div>
<table><tr><td>September 14</td><td>Classes begin</td></tr><tr><td>November 7-8</td><td>Examinations</td></tr>
<tr><td>October 24-25</td><td>Sports Meeting</td></tr></table>
<div class="item"><div class="title">Winter Quarter 2026</div>
<table><tr><td>November 9</td><td>Classes begin</td></tr><tr><td>January 6</td><td>Examinations</td></tr></table>'''
NOTICE = '''<h3>本科生院 研究生院关于2026年下半年部分节假日及学校活动期间教学安排的温馨提醒</h3>
<p>一、中秋节</p><p>9月25日（周五）停课。</p><p>二、国庆节</p>
<p>10月1日（周四）至3日（周六）、5日（周一）停课。</p>
<p>10月6日（周二）、10月7日（周三）分别与9月20日（周日）、10月10日（周六）的课对调。</p>
<p>10月17日（周六）补10月2日（周五）的课，原10月17日（周六）研究生课程照常上课。</p>
<p>三、浙江大学学生节</p><p>12月31日（周四）停课，安排在2027年1月4日（周一）补课。</p>
<p>四、课程调补说明</p>'''


class AcademicTests(unittest.TestCase):
    def test_weighted_gpa_zero_missing_nonpositive_and_terms(self):
        rows = [{'term':'2026-2027-1','credits':'3','gpa':'4.5'},
                {'term':'2026-2027-1','credits':'1','gpa':'0'},
                {'term':'2025-2026-2','credits':'2','gpa':'','score':'优秀'},
                {'credits':'0','gpa':'5'}, {'credits':'not a number','gpa':'5'}]
        result = grade_summary({'data': rows, 'complete': False})
        self.assertEqual(result['overall']['gpa'], 3.38)
        self.assertEqual(result['overall']['credits'], 4)
        self.assertEqual(result['overall']['excluded'], 3)
        self.assertIsNone(next(t for t in result['terms'] if t['term']=='2025-2026-2')['gpa'])
        self.assertFalse(result['complete'])
        self.assertIsNone(grade_summary(None)['overall']['gpa'])

    def test_verified_week_filters_seasons_and_explicit_times(self):
        result = agenda(COURSES, {'2026/3': CONFIG}, '2026-10-09')
        self.assertTrue(result['dated'])
        self.assertEqual(len(result['events']), 1)
        event = result['events'][0]
        self.assertEqual(event['date'], '2026-10-09')
        self.assertEqual(event['time'], '08:00–09:35')
        self.assertEqual(event['week'], 4)
        self.assertTrue(event['start'].endswith('+08:00'))
        winter = agenda(COURSES, {'2026/3': CONFIG}, '2026-11-13')
        self.assertEqual(winter['events'], [])

    def test_holiday_and_makeup_use_original_weekday_and_parity(self):
        courses = {'terms': [COURSES['terms'][0] | {'data': [ROW | {'weekday':'1'},
                           ROW | {'weekday':'3','odd_even':'1'}, ROW | {'weekday':'6','name':'原周六课程'}]}]}
        result = agenda(courses, {'2026/3': CONFIG}, '2026-10-10')
        self.assertFalse(any(e['date']=='2026-10-05' for e in result['events']))
        makeup = [e for e in result['events'] if e['date']=='2026-10-10']
        self.assertEqual(len(makeup), 1)
        self.assertEqual(makeup[0]['teaching_date'], '2026-10-07')
        self.assertEqual(makeup[0]['weekday'], '3')

    def test_unknown_calendar_unknown_rule_and_conflicts_are_visible(self):
        self.assertFalse(agenda(COURSES, {}, '2026-10-09')['dated'])
        cached = agenda(COURSES, {'2026/3': CONFIG | {'stale': True}}, '2026-10-09')
        self.assertTrue(cached['dated'])
        self.assertTrue(cached['events'][0]['provisional'])
        course = COURSES['terms'][0] | {'data': [ROW, ROW | {'name':'冲突课程'}, ROW | {'weeks':'看通知'}]}
        result = agenda({'terms':[course]}, {'2026/3': CONFIG}, '2026-10-09')
        self.assertEqual(len(result['unplaced']), 1)
        self.assertTrue(all(e['conflict'] for e in result['events']))
        self.assertIn('2026-10-24', agenda(COURSES, {'2026/3': CONFIG}, '2026-10-24')['uncertain_dates'])
        self.assertIn('2027-01-04', agenda(COURSES, {'2026/3': CONFIG}, '2027-01-04')['uncertain_dates'])

    def test_current_selection_is_not_inferred_from_newest_year(self):
        future = COURSES['terms'][0] | {'year':'2027', 'current':False}
        self.assertEqual(agenda({'terms':[future,COURSES['terms'][0]]}, {}, '2026-10-09')['term_key'], '2026/3')
        self.assertEqual(agenda({'terms':[future]}, {}, '2026-10-09')['term_key'], '')

    def test_periods_week_lists_and_unknown_syntax(self):
        self.assertEqual(course_periods({'periods':'1-2'}), (1,2))
        self.assertIsNone(course_periods({'start_period':'0','duration':'2'}))
        self.assertTrue(week_matches({'weeks':'1-8周(双)','odd_even':'1'}, 4))
        self.assertFalse(week_matches({'weeks':'1,3,5周'}, 4))
        self.assertIsNone(week_matches({'weeks':'按通知'}, 4))
        self.assertIsNone(week_matches({}, 4))

    def test_quarter_week_numbers_follow_source_season(self):
        term = COURSES['terms'][0] | {'data':[ROW | {'season':'冬'}]}
        result = agenda({'terms':[term]}, {'2026/3':CONFIG}, '2026-11-13')
        self.assertEqual(len(result['events']), 1)
        self.assertEqual(result['events'][0]['week'], 1)
        self.assertEqual(result['unplaced'], [])
        term['data'][0]['weeks'] = '1-16'
        result = agenda({'terms':[term]}, {'2026/3':CONFIG}, '2026-11-13')
        self.assertEqual(len(result['events']), 1)


class PublicCalendarTests(unittest.TestCase):
    def test_program_parses_years_dates_times_and_explicit_exchanges(self):
        seasons = quarters(ENGLISH_FIXTURE, 2026, '3')
        self.assertEqual(seasons[1]['exam_start'], '2027-01-06')
        text = ' '.join(a.replace(':','：')+'一'+b.replace(':','．') for a,b in TIMES)
        self.assertEqual(period_times(text), TIMES)
        holidays, exchanges = teaching_changes(NOTICE, 2026)
        self.assertIn('2026-10-05', holidays)
        self.assertEqual(exchanges['2026-10-10'], '2026-10-07')
        self.assertEqual(exchanges['2026-10-07'], '2026-10-10')
        self.assertEqual(exchanges['2027-01-04'], '2026-12-31')

    def test_unknown_shape_rules_or_missing_year_are_rejected(self):
        for text in ('08:00-08:45', ' '.join(a+'-'+b for a,b in TIMES[:-1])):
            with self.assertRaises(ValueError): period_times(text)
        with self.assertRaises(ValueError): quarters(ENGLISH_FIXTURE, 2025, '3')
        with self.assertRaises(ValueError): teaching_changes(NOTICE.replace('四、课程调补说明','另行通知'), 2026)
        with self.assertRaises(ValueError): teaching_changes(NOTICE.replace('三、浙江大学学生节','课程临时调课。三、浙江大学学生节'), 2026)

    def test_runtime_discovery_crosscheck_provenance_and_cache(self):
        client = CalendarClient()
        article = 'https://ugrs.zju.edu.cn/2026/0710/c28218a1/page.htm'
        pdf = 'https://ugrs.zju.edu.cn/_upload/article/files/test.pdf'
        notice = 'https://zdbk.zju.edu.cn/jwglxt/xtgl/xwck_ckLoginNews.html?xwbh='+'A'*32
        pages = [{'notes':'2026年9月14日11月7一8日11月9日2027年1月6日',
                  'times':' '.join(a+'-'+b for a,b in TIMES)}]*2
        sources = {CATALOG: '<a href="'+article+'">浙江大学2026—2027学年校历</a>',
                   article: '<div pdfsrc="'+pdf+'"></div>', pdf: '%PDF-synthetic',
                   ENGLISH: ENGLISH_FIXTURE,
                   NEWS: '<a href="'+notice+'">本科生院关于2026年下半年部分节假日...</a>', notice: NOTICE}
        with patch.object(client, 'read', side_effect=lambda u,*a,**kw: sources[u].encode()), patch(
                'connectors.campus.calendar_sources.subprocess.run', return_value=SimpleNamespace(
                    returncode=0, stdout=json.dumps(pages).encode())):
            result = client.fetch('2026','3')
        self.assertTrue(result['verified'])
        self.assertEqual(result['sources'][0]['url'], pdf)
        self.assertEqual(len(result['sources'][0]['sha256']), 64)
        self.assertIn('2026-10-24', result['uncertain_dates'])
        with patch.object(client, 'read', side_effect=AssertionError('cached public source must not refetch')):
            self.assertEqual(client.fetch('2026','3'), result)
        wrong_year_pages = [p | {'notes': p['notes'].replace('2026年','2024年')} for p in pages]
        with patch.object(client, 'read', side_effect=lambda u,*a,**kw: sources[u].encode()), patch(
                'connectors.campus.calendar_sources.subprocess.run', return_value=SimpleNamespace(
                    returncode=0, stdout=json.dumps(wrong_year_pages).encode())):
            with self.assertRaises(CampusError): client.fetch('2026','3',force=True)
        with self.assertRaises(CampusError): client.fetch('2026','12')

    def test_runtime_conflict_never_marks_calendar_verified(self):
        with self.assertRaises(ValueError):
            period_times(' '.join(a+'-'+b for a,b in TIMES).replace('08:45','08:46'))

    def test_public_reader_rejects_arbitrary_paths_ports_and_domains(self):
        client = CalendarClient()
        for url in ('http://ugrs.zju.edu.cn/28218/list1.htm', 'https://evil.example/calendar.pdf',
                    'https://zdbk.zju.edu.cn/jwglxt/delete.html', 'https://www.zju.edu.cn/private',
                    'https://ugrs.zju.edu.cn:bad/28218/list1.htm',
                    'https://user:secret@ugrs.zju.edu.cn/28218/list1.htm'):
            with self.assertRaises(CampusError): client.read(url)


if __name__ == '__main__': unittest.main()

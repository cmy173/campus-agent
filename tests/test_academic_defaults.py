"""Real ZDBK parameter shapes and cache/default degradation, using synthetic data."""
from datetime import date
import unittest
from unittest.mock import patch

from campus_agent.academic_defaults import default_calendar, fallback_terms, resolve_calendar, semester_order
from campus_agent.academics import agenda
from campus_agent.campus import CampusClient, CampusError, TIMETABLE
from test_academics import CONFIG, ROW
import test_campus as campus_fixture


class DefaultCalendarTests(unittest.TestCase):
    def test_source_single_chinese_suffix_is_opaque_and_does_not_infer_season(self):
        client=CampusClient()
        html='''<select name="xnm"><option value="2026">2026-2027</option></select>
            <select name="xqm"><option value="1|一">第一学期</option></select>'''
        with patch.object(client,'request',return_value=(200,{},html.encode())):terms=client.terms()
        self.assertEqual(terms[0]['semester'],'1|一');self.assertFalse(client.term_warnings)
        self.assertIsNone(default_calendar(terms[0]))
        with patch.object(client,'json_request',return_value={'kbList':[]}) as sent:client.timetable('2026','1|一')
        self.assertEqual(sent.call_args.args[1]['xqm'],'1|一')

    def test_three_character_numeric_pairs_are_preserved_and_not_guessed(self):
        client=CampusClient()
        html='''<select name="xnm"><option selected value="2026">2026-2027</select>
            <select name="xqm"><option value="0|0">全年</option>
            <option selected value="1|0">秋冬</option><option value="2|0">春夏</option><option value="3|0">短学期</option></select>'''
        with patch.object(client, 'request', return_value=(200, {}, html.encode())):terms=client.terms()
        self.assertEqual({t['semester'] for t in terms}, {'1|0','2|0','3|0'})
        self.assertFalse(client.term_warnings)
        self.assertEqual(terms[0]['semester'], '1|0')
        for term in terms:
            with patch.object(client, 'json_request', return_value={'kbList': []}) as sent:
                client.timetable(term['query_year'], term['semester'])
            self.assertEqual(sent.call_args.args[1]['xqm'], term['semester'])
        self.assertEqual(default_calendar(terms[0])['seasons'][0]['name'], '秋')
        self.assertIsNone(default_calendar({'year':'2026','semester':'3|0','term':'未知季节'}))

    def test_season_suffixes_and_combined_source_codes_preserve_query(self):
        client = CampusClient()
        html = '''<select name="xnm"><option selected value="2026-2027">2026-2027</select>
            <select name="xqm"><option value="0|全年"> 全 年 </option>
            <option selected value="1|秋学期">秋学期</option><option value="1|冬季">冬季</option>
            <option value="12|春夏学期">春夏学期</option></select>'''
        with patch.object(client, 'request', return_value=(200, {}, html.encode())):
            terms = client.terms()
        self.assertEqual(len(terms), 3); self.assertFalse(client.term_warnings)
        self.assertEqual(terms[0]['semester'], '1|秋学期')
        self.assertEqual(semester_order('12|春夏学期')[0], 4)
        with patch.object(client, 'json_request', return_value={'kbList': []}) as sent:
            client.timetable(terms[0]['query_year'], terms[0]['semester'])
        self.assertEqual(sent.call_args.args[1]['xqm'], '1|秋学期')

    def test_season_suffixes_use_correct_calendar_and_quarter_coverage(self):
        terms = [{'year':'2026','semester':code,'term':code,'data':[ROW | {'season':''}]}
                 for code in ('1|冬学期', '1|秋季学期', '12|春夏学期')]
        calendars = {t['year']+'/'+t['semester']:default_calendar(t) for t in terms}
        result = agenda({'terms':terms},calendars,'2026-10-09')
        self.assertEqual(result['term_key'], '2026/1|秋季学期')
        self.assertEqual(len(result['events']), 1)
        self.assertEqual(calendars['2026/12|春夏学期']['seasons'][0]['name'], '春')

    def test_default_dates_times_empty_adjustments_and_missing_weeks(self):
        term = {'year':'2026','semester':'1|秋','term':'2026-2027 · 秋','current':True,
                'data':[ROW | {'weeks':'','odd_even':'','season':''}]}
        config = default_calendar(term)
        self.assertFalse(config['verified'])
        self.assertEqual(config['seasons'][0]['start'],'2026-09-14')
        self.assertEqual(config['seasons'][1]['start'],'2026-11-09')
        self.assertEqual(config['holidays'],[])
        self.assertEqual(config['exchanges'],{})
        result = agenda({'terms':[term]}, {'2026/1|秋':config}, '2026-10-09')
        self.assertTrue(result['dated'])
        self.assertEqual(result['events'][0]['time'],'08:00–09:35')
        self.assertTrue(result['events'][0]['provisional'])
        self.assertEqual(len(result['events'][0]['assumptions']),2)
        self.assertEqual(result['events'][0]['name'],ROW['name'])

    def test_exact_cache_keeps_dates_but_other_year_only_supplies_times(self):
        term = {'year':'2026','semester':'3','term':'秋冬'}
        cached = CONFIG | {'stale':True,'fetched_at':'2026-10-01T00:00:00+08:00'}
        result = resolve_calendar(term, {'2026/3':cached})
        self.assertEqual(result['source_status'],'cache')
        self.assertEqual(result['seasons'],CONFIG['seasons'])
        fallback = resolve_calendar(term, {'2025/3':cached})
        self.assertEqual(fallback['source_status'],'fallback')
        self.assertEqual(fallback['holidays'],[])
        self.assertEqual(fallback['period_times'],CONFIG['period_times'])
        self.assertIsNone(default_calendar({'year':'2026','semester':'99','term':'未知'}))
        # A deployment's explicit label wins over the usual numeric-code convention.
        spring = default_calendar({'year':'2026','semester':'1','term':'2026-2027 · 春夏'})
        self.assertEqual(spring['seasons'][0]['name'],'春')
        self.assertEqual(spring['seasons'][0]['start'],'2027-02-22')

    def test_quarter_selection_does_not_choose_winter_during_autumn(self):
        terms = [{'year':'2026','semester':code,'term':code,'data':[ROW | {'season':code[-1]}]}
                 for code in ('1|冬','1|秋')]
        calendars = {t['year']+'/'+t['semester']:default_calendar(t) for t in terms}
        result = agenda({'terms':terms},calendars,'2026-10-09')
        self.assertEqual(result['term_key'],'2026/1|秋')
        self.assertEqual(len(result['events']),1)

    def test_fallback_range_uses_grade_history_and_does_not_invent_courses(self):
        terms = fallback_terms({'grades':{'data':[{'term':'2023-2024-1'}]}},date(2026,10,9))
        self.assertEqual({t['year'] for t in terms},{'2023','2024','2025','2026'})
        self.assertTrue(all('data' not in t and t['range_source']=='fallback' for t in terms))
        self.assertTrue(any(t['current'] and t['semester']=='1|秋' for t in terms))
        self.assertEqual(len(fallback_terms({},date(2026,10,9))),32)

    def test_actual_year_ranges_and_quarter_codes_are_sent_unchanged(self):
        client = CampusClient()
        html = '''<select name="xnm"><option selected value="2026-2027">2026-2027</select>
                  <select name="xqm"><option selected value="1|秋">秋</option><option value="1|冬">冬</select>'''
        with patch.object(client,'page_request',return_value=html):
            terms = client.terms()
        self.assertEqual(terms[0]['year'],'2026')
        self.assertEqual(terms[0]['query_year'],'2026-2027')
        self.assertEqual(terms[0]['semester'],'1|秋')
        with patch.object(client,'json_request',return_value={'kbList':[]}) as request:
            client.timetable(terms[0]['query_year'],terms[0]['semester'])
        self.assertEqual(request.call_args.args,(TIMETABLE,{'xnm':'2026-2027','xqm':'1|秋','captcha_value':''}))

    def test_bad_row_does_not_discard_valid_courses_or_accept_bad_envelope(self):
        client = CampusClient()
        good = {'kcmc':'合成课','xqj':'5','djj':'1','skcd':'2','xxq':'秋','dsz':'2'}
        with patch.object(client,'json_request',return_value={'kbList':[good,{},None],'xsxx':{'XH':'synthetic','XM':'合成'}}):
            profile, rows = client.timetable('2026-2027','1|秋')
        self.assertEqual(len(rows),1)
        self.assertEqual(profile['name'],'合成')
        self.assertEqual(client.timetable_warnings[0]['skipped'],2)
        with patch.object(client,'json_request',return_value={'items':[]}):
            with self.assertRaises(CampusError):client.timetable('2026-2027','1|秋')


class DegradedSyncTests(unittest.TestCase):
    def test_numeric_pair_terms_can_be_displayed_and_calendar_updated(self):
        self.connect()
        fake=next(iter(self.manager.states.values()))['client']
        fake.catalog=[{'year':'2026','semester':'1|0','term':'2026-2027 · 秋冬','current':True}]
        self.sync()
        view=self.client.get('/api/campus/academics',params={'day':'2026-10-09','term':'2026/1|0'})
        self.assertEqual(view.status_code,200,view.text)
        self.assertEqual(view.json()['agenda']['term_key'],'2026/1|0')
        self.assertTrue(view.json()['agenda']['dated'])
        with patch.object(self.manager.calendar_client,'fetch',side_effect=CampusError('CALENDAR_UNAVAILABLE','暂不可用')):
            response=self.client.post('/api/campus/calendar',json={'year':'2026','semester':'1|0','campus':'main'})
        self.assertEqual(response.status_code,200,response.text)
        self.assertFalse(response.json()['calendar_result']['verified'])

    setUp = campus_fixture.CampusWebTests.setUp
    tearDown = campus_fixture.CampusWebTests.tearDown
    register = campus_fixture.CampusWebTests.register
    connect = campus_fixture.CampusWebTests.connect
    sync = campus_fixture.CampusWebTests.sync

    def test_original_query_values_and_partial_failure_allow_remaining_terms(self):
        self.connect()
        fake = next(iter(self.manager.states.values()))['client']
        fake.catalog = [{'year':'2026','query_year':'2026-2027','semester':code,'term':code,'current':i==0}
                        for i,code in enumerate(('1|秋','1|冬'))]
        original = fake.timetable
        def query(year,semester,captcha):
            if semester=='1|冬':raise CampusError('PROTOCOL_CHANGED','本学期未开放')
            return original(year,semester,captcha)
        with patch.object(fake,'timetable',side_effect=query):result=self.sync().json()
        self.assertEqual(fake.queried,[('2026-2027','1|秋')])
        self.assertFalse(result['snapshot']['courses']['complete'])
        self.assertEqual(len(result['snapshot']['sync_progress']['failed']),1)
        self.assertIsNotNone(result['snapshot']['profile'])
        view=self.client.get('/api/campus/academics?day=2026-10-09&term=2026/1%7C秋').json()
        self.assertTrue(view['agenda']['dated'])
        self.assertEqual(view['agenda']['source_status'],'fallback')
        fake.catalog.append({'year':'2026','semester':'99','term':'未知','current':False})
        self.sync()
        with patch.object(self.manager.calendar_client,'fetch',side_effect=CampusError('CALENDAR_UNSUPPORTED','暂无适配')):
            response = self.client.post('/api/campus/calendar',json={'year':'2026','semester':'99','campus':'main'})
        self.assertEqual(response.status_code,200,response.text)
        self.assertFalse(response.json()['calendar_result']['verified'])
        self.assertFalse(self.client.get('/api/campus/academics?term=2026/99').json()['agenda']['dated'])

    def test_first_sync_list_failure_queries_real_data_using_new_grade_history(self):
        self.connect()
        fake = next(iter(self.manager.states.values()))['client']
        fake.discovery_error = CampusError('TERM_DISCOVERY_FAILED','页面选项缺失')
        result = self.sync().json()
        self.assertEqual(result['snapshot']['sync_progress']['total'],8)
        self.assertTrue(result['auto_continue'])
        final = self.client.post('/api/campus/sync',json={'resume':True}).json()
        self.assertFalse(final['auto_continue'])
        self.assertFalse(final['snapshot']['courses']['complete'])
        self.assertEqual(final['snapshot']['sync_progress']['completed'],8)
        self.assertEqual({y for y,s in fake.queried},{'2025-2026','2026-2027'})
        self.assertEqual(final['snapshot']['profile']['data']['name'],campus_fixture.PROFILE['name'])

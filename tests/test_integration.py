"""Regressions for branch integration; never contact campus services."""
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch
from fastapi.testclient import TestClient
from campus_agent.web import create_app
from campus_agent.academics import agenda
from test_academics import CONFIG, ROW, COURSES


class IntegrationTests(unittest.TestCase):
    def test_new_local_user_can_register_without_campus_network(self):
        with TemporaryDirectory() as folder, TestClient(create_app(folder), base_url='http://127.0.0.1') as client:
            client.headers['X-Campus-Request'] = '1'
            with patch('campus_agent.campus.CampusClient.prepare', side_effect=AssertionError('Unexpected campus access')):
                result = client.post('/api/auth/register', json={'username':'local_trial','password':'independent-local-secret'})
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(client.get('/api/me').json()['mode'], 'legacy')
                self.assertEqual(client.get('/api/documents').json(), [])
                self.assertEqual(client.get('/api/campus/academics').status_code, 200)

    def test_verified_calendar_does_not_turn_unknown_weeks_into_weekly_courses(self):
        term = COURSES['terms'][0] | {'data':[ROW | {'weeks':'', 'odd_even':''}, ROW | {'name':'明确周次'}]}
        result = agenda({'terms':[term]}, {'2026/3':CONFIG}, '2026-10-09')
        self.assertEqual([r['name'] for r in result['events']], ['明确周次'])
        self.assertEqual([r['name'] for r in result['unplaced']], [ROW['name']])

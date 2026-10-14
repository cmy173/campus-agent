"""Verify actual urllib request routing without contacting school services."""
import os
import unittest
import urllib.request
from unittest.mock import patch
from urllib.parse import urlsplit

from campus_agent.campus import CampusClient
from campus_agent.calendar_sources import CalendarClient


class TransportReached(Exception):
    """Stop immediately before urllib would create a network connection."""


class CampusNetworkTests(unittest.TestCase):
    def assert_direct(self, opener, url):
        with patch.object(urllib.request.HTTPSHandler, 'do_open',
                          side_effect=TransportReached) as transport:
            with self.assertRaises(TransportReached):
                opener.open(url)
        request = transport.call_args.args[1]
        self.assertEqual(request.host, urlsplit(url).hostname)
        self.assertIsNone(request._tunnel_host)
        self.assertFalse(request.has_header('Proxy-authorization'))

    def check_campus_routes(self):
        campus = CampusClient()
        calendar = CalendarClient()
        for url in (
            'https://zjuam.zju.edu.cn/cas/login',
            'https://zjuam.zju.edu.cn/cas/kaptcha',
            'https://zdbk.zju.edu.cn/jwglxt/',
            'https://eta.zju.edu.cn/',
        ):
            with self.subTest(url=url):
                self.assert_direct(campus.opener, url)
        for url in (
            'https://www.zju.edu.cn/english/19600/list.htm',
            'https://ugrs.zju.edu.cn/28218/list1.htm',
            'https://zdbk.zju.edu.cn/jwglxt/',
        ):
            with self.subTest(url=url):
                self.assert_direct(calendar.opener, url)

    def test_environment_proxy_is_bypassed_for_campus_only(self):
        with patch.dict(os.environ, {
            'http_proxy': 'http://127.0.0.1:18765',
            'https_proxy': 'http://127.0.0.1:18765',
        }), patch('urllib.request.proxy_bypass', return_value=False):
            self.check_campus_routes()
            # Other clients still inherit the proxy (e.g. the OpenAI adapter).
            opener = urllib.request.build_opener()
            with patch.object(urllib.request.HTTPSHandler, 'do_open',
                              side_effect=TransportReached) as transport:
                with self.assertRaises(TransportReached):
                    opener.open('https://api.openai.com/v1/responses')
            request = transport.call_args.args[1]
            self.assertEqual(request.host, '127.0.0.1:18765')
            self.assertEqual(request._tunnel_host, 'api.openai.com')

    def test_system_proxy_discovery_is_not_used(self):
        with patch('urllib.request.getproxies', return_value={
            'https': 'http://127.0.0.1:18765',
        }) as discovery, patch('urllib.request.proxy_bypass', return_value=False):
            self.check_campus_routes()
            discovery.assert_not_called()


if __name__ == '__main__':
    unittest.main()

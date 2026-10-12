"""Opt-in real public verification. Never run as part of automated tests."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from campus_agent.campus import CampusManager
from campus_agent.connector_contracts import UserContext, NoticesQuery
from campus_agent.core import TZ
from campus_agent.storage import Store
from campus_agent.campus_network import network_policy


def main():
    parser = argparse.ArgumentParser(description='Read fixed public ZDBK notice metadata without campus credentials')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    with TemporaryDirectory(prefix='campus-public-verification-') as temporary:
        store = Store(Path(temporary)/'campus.sqlite3')
        with store.connect() as db:
            db.execute('INSERT INTO users VALUES(?,?,?)', ('verification','local-verification','unused-synthetic-local-value'))
        manager = CampusManager(store)
        context = UserContext('verification')
        service = manager.service
        result = service.sync('notices', NoticesQuery(), context)
        adapter = service.registry.adapters['zdbk_public']
        with patch('urllib.request.OpenerDirector.open',side_effect=AssertionError('Offline query must not contact the platform')):
            offline = service.query('notices',NoticesQuery(),context)
            service.disconnect('zdbk_public',context)
            disconnected = service.query('notices',NoticesQuery(),context)
            reopened = CampusManager(Store(Path(temporary)/'campus.sqlite3'))
            persisted = reopened.service.query('notices',NoticesQuery(),context)
            reopened.close()
        report = {'verified_at':datetime.now(TZ).isoformat(), 'network_policy':network_policy().value,
            'verification_kind':'real public read; synthetic local owner; no campus authentication',
            'capability':'notices', 'platform':result.platform, 'record_count':len(result.records),
            'fetched_at':result.fetched_at, 'completeness':result.completeness,
            'error':result.error.model_dump(mode='json') if result.error else None,
            'offline_query_preserved':offline.records == result.records,
            'disconnect_preserved':disconnected.records == result.records,
            'restart_preserved':persisted.records == result.records,
            'sources':[record['source'] for record in result.records],
            'not_verified':['campus account login after migration','notice bodies and history','personal notices','other networks or policies']}
        manager.close()
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,ensure_ascii=True,indent=2))
    return 1 if report['error'] or not report['record_count'] or not all(report[k] for k in
        ('offline_query_preserved','disconnect_preserved','restart_preserved')) else 0


if __name__ == '__main__':raise SystemExit(main())

"""Opt-in public CKC verification with a synthetic profile and temporary database."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from campus_agent.campus import CampusManager
from campus_agent.campus_college_sources import CKC
from campus_agent.connector_contracts import UserContext, CollegeNoticesQuery
from campus_agent.core import TZ
from campus_agent.storage import Store


def main():
    parser = argparse.ArgumentParser(description='Read CKC public notices without any campus credentials')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    with TemporaryDirectory(prefix='college-notice-verification-') as directory:
        store = Store(Path(directory)/'campus.sqlite3')
        with store.connect() as db:
            db.execute('INSERT INTO users VALUES(?,?,?)',('verification','synthetic-local-user','unused'))
        manager = CampusManager(store)
        context = UserContext('verification')
        manager.repository.write(context.owner, {'profile':{'data':{'college':CKC.college},
            'fetched_at':datetime.now(TZ).isoformat(), 'stale':False}})
        result = manager.service.sync('college_notices',CollegeNoticesQuery(),context)
        with patch('urllib.request.OpenerDirector.open',side_effect=AssertionError('Offline query contacted network')):
            offline = manager.service.query('college_notices',CollegeNoticesQuery(),context)
            manager.service.disconnect('ckc_public',context)
            disconnected = manager.service.query('college_notices',CollegeNoticesQuery(),context)
            reopened = CampusManager(store)
            persisted = reopened.service.query('college_notices',CollegeNoticesQuery(),context)
            reopened.close()
        report = {'verified_at':datetime.now(TZ).isoformat(),
            'verification_kind':'real credential-free public pages; synthetic profile and local owner',
            'network_policy':os.environ.get('CAMPUS_PUBLIC_NETWORK_POLICY','system'),
            'platform':result.platform, 'capability':'college_notices', 'source':CKC.listing,
            'selection_status':result.selection.status, 'record_count':len(result.records),
            'body_characters':[len(r['body']) for r in result.records],
            'attachment_counts':[len(r['attachments']) for r in result.records],
            'sources':[r['source'] for r in result.records],
            'fetched_at':result.fetched_at, 'completeness':result.completeness,
            'error':result.error.model_dump(mode='json') if result.error else None,
            'offline_query_preserved':offline.records==result.records,
            'disconnect_preserved':disconnected.records==result.records,
            'restart_preserved':persisted.records==result.records,
            'not_verified':['live campus login/profile refresh','other colleges','historical pagination',
                'attachment or image contents','deadline accuracy on a labelled real corpus','other network environments']}
        manager.close()
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,ensure_ascii=True,indent=2))
    return 1 if report['error'] or not report['record_count'] or not all(report[k] for k in
        ('offline_query_preserved','disconnect_preserved','restart_preserved')) else 0


if __name__ == '__main__':
    raise SystemExit(main())

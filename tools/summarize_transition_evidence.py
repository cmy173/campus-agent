"""Summarize already executed evidence; never calls a model or a school service."""
from pathlib import Path
import json
import hashlib
import xml.etree.ElementTree as ET
from audit_live_transition import visibility

ROOT = Path(__file__).resolve().parents[1]
directory = ROOT / 'docs/audit'
summary = {'kind': 'observed-results-not-statistical-causal-evaluation', 'live_runs': []}
for name in ('transition-live-model.json', 'transition-live-notices-after.json', 'transition-live-notices-final.json'):
    path = directory / name
    report = json.loads(path.read_text(encoding='utf-8'))
    for result in report['results']:
        trace = Path(report['scratch']) / (result['id'] + '-trace.json')
        events = json.loads(trace.read_text(encoding='utf-8'))
        cid = next(event['data']['session_id'] for event in events if event['kind'] == 'harness_event')
        result.update(visibility(Path(report['scratch']), cid))
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    results = report['results']
    usage = [u for result in results for u in result.get('provider_usage') or []]
    summary['live_runs'].append({'evidence': name, 'tasks': len(results),
        'model_budget': report.get('model_budget', 8),
        'terminal_succeeded': sum(r['status'] == 'succeeded' for r in results),
        'tool_calls': sum(len(r['actual_tools']) for r in results),
        'tool_errors': sum(outcome['is_error'] for r in results for outcome in r['tool_outcomes']),
        'model_calls': sum(r['model_calls'] for r in results),
        'reported_conversation_tokens': sum(u.get('totalTokens', 0) for u in usage),
        'search_auxiliary_usage_included': False, 'currency_cost': None,
        'human_interventions': sum(r['human_interventions'] for r in results),
        'elapsed_ms': sum(r['elapsed_ms'] for r in results)})
paths = ['apps/api/app/agent_bridge/tools.py', 'apps/api/app/agent_bridge/store.py',
         'apps/api/app/agent_bridge/runtime.py', 'apps/api/app/agent_bridge/job_actions.py',
         'apps/api/app/agent_bridge/diagnostics.py', 'apps/api/app/routers/agent.py',
         'apps/api/app/routers/connections.py', 'apps/agent-runtime/campus-plugin.mjs',
         'apps/agent-runtime/diagnose.mjs', 'apps/agent-runtime/browser-action-policy.mjs',
         'apps/agent-runtime/web-recovery.mjs', 'apps/api/tests/test_agent_transition_audit.py']
summary['source_sha256'] = {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in paths}
summary['automated_regressions'] = {
    label: ET.parse(directory / filename).getroot()[0].attrib
    for label, filename in [('api', 'transition-api-final.xml'), ('connectors', 'transition-connectors-final.xml')]
    if (directory / filename).exists()
}
(directory / 'transition-summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(summary['live_runs'], ensure_ascii=False))

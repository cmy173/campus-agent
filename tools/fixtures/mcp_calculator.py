"""Real stdio MCP fixture: arithmetic, error reporting and one-time disconnect.

Used only by the runtime integration verifier, with its own temporary audit path.
No model calls or credentials. The upstream SDK performs discovery/transport.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

audit = Path(sys.argv[1])
drop_marker = audit.with_suffix('.disconnected')


def send(identifier, result):
    print(json.dumps({'jsonrpc': '2.0', 'id': identifier, 'result': result}), flush=True)


for line in sys.stdin:
    request = json.loads(line)
    with audit.open('a', encoding='utf-8') as log:
        log.write(json.dumps({'pid': os.getpid(), 'method': request.get('method'), 'params': request.get('params')}) + '\n')
    if 'id' not in request:
        continue
    identifier, method = request['id'], request.get('method')
    if method == 'initialize':
        send(identifier, {'protocolVersion': '2025-06-18', 'capabilities': {'tools': {}},
                          'serverInfo': {'name': 'campus-runtime-verifier', 'version': '1.0.0'}})
    elif method == 'ping':
        send(identifier, {})
    elif method == 'tools/list':
        send(identifier, {'tools': [{'name': 'multiply', 'description': 'Multiply two integers in a separate local MCP process.',
            'inputSchema': {'type': 'object', 'properties': {'a': {'type': 'integer'}, 'b': {'type': 'integer'},
                'disconnect_once': {'type': 'boolean'}, 'report_error': {'type': 'boolean'}},
                'required': ['a', 'b'], 'additionalProperties': False}}]})
    elif method == 'tools/call':
        args = request.get('params', {}).get('arguments', {})
        if '--require-credential' in sys.argv and os.environ.get('MCP_FIXTURE_CREDENTIAL') != 'fixture-private-581':
            send(identifier, {'isError': True, 'content': [{'type': 'text', 'text': 'Fixture credential did not reach MCP service'}]})
            continue
        if args.get('disconnect_once') and not drop_marker.exists():
            drop_marker.write_text('actual service disconnect', encoding='utf-8')
            sys.exit(23)
        if args.get('report_error'):
            send(identifier, {'isError': True, 'content': [{'type': 'text', 'text': 'Requested calculator failure'}]})
        elif isinstance(args.get('a'), int) and isinstance(args.get('b'), int):
            send(identifier, {'content': [{'type': 'text', 'text': 'MCP computed: ' + str(args['a'] * args['b'])}]})
        else:
            send(identifier, {'isError': True, 'content': [{'type': 'text', 'text': 'Two integers are required'}]})
    else:
        print(json.dumps({'jsonrpc': '2.0', 'id': identifier, 'error': {'code': -32601, 'message': 'Method not supported'}}), flush=True)

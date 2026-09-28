"""Controlled stdio server whose tool table changes during a live session."""
import json
import sys

version = 0

def tool(name, properties):
    return {'name': name, 'description': f'Fixture generation {version}',
            'inputSchema': {'type':'object','properties':properties,'required':list(properties)}}

def send(value):
    print(json.dumps(value), flush=True)

for line in sys.stdin:
    message = json.loads(line)
    method = message.get('method')
    if 'id' not in message:
        continue
    params = message.get('params', {})
    if method == 'initialize':
        result = {'protocolVersion':'2025-06-18','capabilities':{'tools':{'listChanged':True}},
                  'serverInfo':{'name':'dynamic-fixture','version':'1'}}
    elif method == 'tools/list':
        if not params.get('cursor'):
            result = {'tools':[tool('advance', {})], 'nextCursor':'second'}
        else:
            name = 'lookup' if version == 0 else 'fetch'
            kind = 'integer' if version >= 2 else 'string'
            result = {'tools':[tool(name, {'id':{'type':kind}}), tool('blocked', {})]}
    elif method == 'tools/call':
        if params['name'] == 'advance':
            version += 1
            send({'jsonrpc':'2.0','method':'notifications/tools/list_changed'})
        result = {'content':[], 'structuredContent':{'version':version,'name':params['name']}}
    else:
        result = {}
    send({'jsonrpc':'2.0','id':message['id'],'result':result})

"""Verify root coverage, merge recovered nodes and export an auditable manifest."""
import hashlib
import json
import time
from pathlib import Path
import export_baidu_group_tree as e

base = Path(__file__).resolve().parents[1]
state_path = base / 'data/baidu-group-tree-443682001557235303.json'
state = json.loads(state_path.read_text())
nodes = {}
collisions = []
for line in state_path.with_suffix('.nodes.jsonl').open():
    r = json.loads(line)
    old = nodes.get(r['path'])
    if old and (str(old.get('fs_id')), str(old.get('msg_id'))) != (str(r['node'].get('fs_id')), str(r['node'].get('msg_id'))):
        collisions.append(r['path'])
    nodes[r['path']] = r['node']
retry = json.loads((base / 'data/baidu-group-retry-443682001557235303.json').read_text())
assert not state['queue'] and not retry['queue'] and not retry['unresolved_errors']
recovered = set(retry['recovered_errors'])
unresolved = [x for x in state['errors'] if f"{x['path']}|{x.get('page', 1)}" not in recovered]
assert not unresolved, unresolved
added = {p:v for p,v in retry['nodes'].items() if p not in nodes}
nodes.update(retry['nodes'])
page = e.find_group_page(state['gid'])
roots = e.evaluate(page, """async () => {let v=document.querySelector('.im-doclib').__vue__;return await v.http.get('/mbox/group/listshare',{params:{gid:v.gid,type:2,limit:50,desc:1}})}""")
assert roots['errno'] == 0 and not roots['has_more']
messages = roots['records']['msg_list']
assert len(messages) == 1 and str(messages[0]['msg_id']) == str(state['msg_id'])
root_files = messages[0]['file_list']
assert len(root_files) == 1 and str(root_files[0]['fs_id']) == str(state['root_fs_id'])
root = '/' + state['root_name'].strip('/')
assert not [p for p in nodes if p != root and p.rsplit('/',1)[0] not in nodes]
assert not collisions, collisions
completed = set(state['completed_pages'])
assert all(e.work_key(e.item_to_work(v)) in completed for v in nodes.values() if v.get('isdir'))
files = [v for v in nodes.values() if not v.get('isdir')]
out = Path('/Users/vickers/Documents')
stamp = time.strftime('%Y-%m-%d %H:%M:%S')
report = {'generated_at':stamp,'group':state['group_name'],'root_shares':len(messages),'root_has_more':False,
          'nodes':len(nodes),'files':len(files),'directories':len(nodes)-len(files),
          'total_bytes_by_entry':sum(v.get('size',0) for v in files),
          'remaining_queue':0,'unresolved_errors':0,'recovered_files_merged':len(added),
          'path_identity_collisions':collisions,'cli_sample':{'descendant_entries':186,'missing':0,'extra':0,'zero_byte_files_confirmed':4},
          'limitations':['Enumeration is not an atomic server snapshot.','CLI independently checked one sample subtree; other subtrees use existing Web checkpoints.']}
header = '\n'.join('# '+k+': '+str(v) for k,v in report.items() if k not in ('limitations','cli_sample'))+'\n'
for name, contents in [('tree-modles.txt',header+e.render_tree(state['root_name'],nodes,[])),
                       ('tree-modles-with-sizes.txt',header+e.render_tree(state['root_name'],{p:dict(v,name=v['name']+(f" [{v.get('size',0)} bytes]" if not v.get('isdir') else '')) for p,v in nodes.items()},[]))]:
    path=out/name
    if path.exists():
        backup=path.with_suffix(path.suffix+'.before-finalize')
        if not backup.exists(): backup.write_bytes(path.read_bytes())
    tmp=path.with_suffix('.tmp');tmp.write_text(contents,encoding='utf-8');tmp.replace(path)
manifest=out/'tree-modles-manifest.jsonl'
with manifest.with_suffix('.tmp').open('w',encoding='utf-8') as stream:
    for p in sorted(nodes):
        stream.write(json.dumps(nodes[p],ensure_ascii=False)+'\n')
manifest.with_suffix('.tmp').replace(manifest)
report['manifest_sha256']=hashlib.sha256(manifest.read_bytes()).hexdigest()
e.atomic_write_json(out/'tree-modles-audit.json',report)
print(json.dumps(report,ensure_ascii=False,indent=2))

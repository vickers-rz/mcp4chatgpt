import tempfile, subprocess, time, json
from pathlib import Path
from mcp4chatgpt import computer_cua_backend as c, computer_backend as n
APP='com.apple.TextEdit'
base={'app_id':APP,'allowed_apps':[APP],'max_elements':200}
def osa(s):
 return subprocess.run(['osascript','-e',s],capture_output=True,text=True,check=True,timeout=20).stdout.strip()
def close(p):
 osa('tell application "TextEdit"\nrepeat with d in documents\nif path of d is '+json.dumps(str(p))+' then\nclose d saving no\nexit repeat\nend if\nend repeat\nend tell')
def state(w): return c.call('get_state',{**base,'window_id':w})
def field(s):
 matches=[e for e in s['elements'] if 'value' in e and 'CUA_' in e['value']]
 if not matches: raise AssertionError(s['elements'])
 return matches[0]
with tempfile.TemporaryDirectory(prefix='mcp-cua-final-') as temp:
 paths=[Path(temp).resolve()/d/'MCP_CUA_Same_Title.txt' for d in ['A','B']]
 try:
  for i,p in enumerate(paths):
   p.parent.mkdir(); p.write_text('CUA_BASE_'+str(i))
   subprocess.run(['open','-g','-a','TextEdit',str(p)],check=True)
  time.sleep(1)
  windows=c.call('get_state',base)['windows']
  same=[w for w in windows if w['title']=='MCP_CUA_Same_Title.txt']
  assert len(same)==2, same
  ids=[w['window_id'] for w in same]; assert len(set(ids))==2
  originals={w:field(state(w))['value'] for w in ids}
  assert set(originals.values())=={'CUA_BASE_0','CUA_BASE_1'}, originals
  for i,w in enumerate(ids):
   s=state(w); e=field(s)
   c.call('type_text',{**base,'window_id':w,'snapshot_id':s['snapshot_id'],'element_id':e['element_id'],'text':'CUA_CHANGED_'+str(i)},effectful=True)
   assert field(state(w))['value']=='CUA_CHANGED_'+str(i)
  assert [field(state(w))['value'] for w in ids]==['CUA_CHANGED_0','CUA_CHANGED_1']
  print('PASS same-title independent CUA writes')
  old=next(w for w,v in originals.items() if v=='CUA_BASE_0')
  close(paths[0]); subprocess.run(['open','-g','-a','TextEdit',str(paths[0])],check=True); time.sleep(.5)
  try: state(old)
  except c.CUABackendError as exc:
   assert exc.code=='stale_window', exc.code
   print('PASS closed/reopened window rejects old token:',exc.code)
  else: raise AssertionError('old token accepted')
 finally:
  for p in paths:
   try: close(p)
   except Exception as exc: print('cleanup:',type(exc).__name__)
  c.stop(); n.stop()

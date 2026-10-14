const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const test = require('node:test');

function element(tag, text = '') {
  return {tag, textContent:text, disabled:false, children:[],
    append(...nodes) {this.children.push(...nodes);},
    replaceChildren(...nodes) {this.children = nodes;}};
}

function harness(api) {
  const nodes = new Map(), uploads = [], calls = [];
  const get = id => {if (!nodes.has(id)) nodes.set(id, element('div'));return nodes.get(id);};
  const ctx = vm.createContext({$:get, el:element, sessionEpoch:0, loggedIn:true, Blob, AbortController,
    setTimeout, clearTimeout, date:value=>value, toast(){}, showView(){}, refresh:async()=>{},
    upload:async(...args)=>uploads.push(args), api:(path,options)=>{calls.push({path,options});return api(path,options);}});
  vm.runInContext(fs.readFileSync('campus_agent/static/college-notices.js','utf8'),ctx);
  return {ctx,get,uploads,calls,run:code=>vm.runInContext(code,ctx)};
}

function result() {
  return {selection:{college:'竺可桢学院',status:'matched',profile_stale:true}, freshness:'fresh', warnings:[],
    records:[{id:'ckc:123',college:'竺可桢学院',title:'<script>合成标题</script>', published_at:'2026-10-10',
      body:'报名截止：2026-10-15 18:00。',source:'https://webplus.zju.edu.cn/_s597/2026/1010/c78853a123/page.psp',
      attachments:[{name:'合成.docx',url:'https://webplus.zju.edu.cn/_upload/article/files/ab/cd/1234/abc.docx'}],has_unread_content:true}]};
}

test('view reads cache only; detail is text and import sends just the record id', async()=>{
  const h=harness(async path=>path==='/api/college-notices/import'
    ? {id:'doc-1',duplicate:false,attachments:1,has_unread_content:true}
    : result());
  await h.run('loadCollegeNotices()');
  assert.equal(h.calls[0].path,'/api/campus/query/college_notices');
  assert.equal(h.calls[0].options.method,undefined);
  const card=h.get('college-notices-list').children[0];
  assert.equal(card.children[0].textContent,'<script>合成标题</script>');
  const detail=card.children.find(c=>c.tag==='details');
  assert.match(detail.children[1].textContent,/报名截止/);
  const attachment=detail.children.find(c=>c.tag==='p'&&c.children.length);
  assert.equal(attachment.children[0].rel,'noopener noreferrer');
  assert.match(detail.children[detail.children.length-1].textContent,/未下载、未解析/);
  const save=card.children.find(c=>c.tag==='button');
  await save.onclick();await save.onclick();
  // The browser no longer assembles the document text or any URL: provenance stays server-side.
  assert.equal(h.uploads.length,0);
  const imports=h.calls.filter(c=>c.path==='/api/college-notices/import');
  assert.equal(imports.length,2);
  assert.equal(imports[0].options.method,'POST');
  assert.equal(JSON.stringify(imports[0].options.json),'{"record_id":"ckc:123"}');
  assert.equal(save.disabled,false);
});

test('manual sync has no college, owner or URL parameters and errors unlock retry',async()=>{
  let fail=true;
  const h=harness(async()=>{if(fail)throw Error('网络不可达');return result();});
  await h.run('loadCollegeNotices(true)');
  assert.equal(h.get('college-notices-sync').disabled,false);
  assert.match(h.get('college-notices-status').textContent,/网络不可达/);
  assert.equal(JSON.stringify(h.calls[0].options.json),' {"query":{}}'.trim());
  fail=false;
  await h.get('college-notices-sync').onclick();
  assert.equal(h.calls[1].path,'/api/campus/sync/college_notices');
  assert.equal(h.get('college-notices-list').children.length,1);
});

test('account change clears previous college and ignores its delayed response',async()=>{
  let resolve;
  const h=harness(()=>new Promise(r=>resolve=r));
  const pending=h.run('loadCollegeNotices(true)');
  h.run('sessionEpoch++; clearCollegeNotices();');
  resolve(result());await pending;
  assert.equal(h.get('college-notices-list').children.length,0);
  assert.doesNotMatch(h.get('college-notices-status').textContent,/竺可桢/);
  assert.equal(h.get('college-notices-sync').disabled,false);
});

test('missing or unsupported college does not enable a fallback sync',async()=>{
  for(const status of ['missing','unsupported','ambiguous']) {
    const h=harness(async()=>({selection:{college:'未知学院',status},records:[],error:{code:'UNSUPPORTED'}}));
    await h.run('loadCollegeNotices()');
    assert.equal(h.get('college-notices-sync').disabled,true);
    assert.equal(h.get('college-notices-list').children.length,0);
    assert.equal(h.calls.length,1);
  }
});

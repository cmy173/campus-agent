const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const test = require('node:test');

function element(tag, text, className) {
  const node = {tag, className: className || '', textContent: text === undefined ? '' : text,
    disabled: false, checked: false, children: [], href: '', rel: '', target: '', type: '',
    append(...nodes) {this.children.push(...nodes);},
    replaceChildren(...nodes) {this.children = nodes;}};
  return node;
}

const EVENT = {ref:'ev1', title:'<b>第一批测试</b>', summary:'面向2024级、2025级在籍本科生',
  audience:['竺可桢学院2024级、2025级在籍本科生'], actions:['在报名系统完成报名'],
  location:'紫金港校区游泳馆', location_needs_attachment:false, attachment_refs:['att_1'],
  times:[
    {kind:'registration_deadline', raw:'2026年10月15日 17:00', value:'2026-10-15T17:00:00+08:00',
     precision:'datetime', derived:true, derivation:'原文 17:00 直接规范化', conflict:false},
    {kind:'assembly', raw:'2026年10月18日 8:30', value:'2026-10-18T08:30:00+08:00',
     precision:'datetime', derived:false, derivation:'', conflict:false},
    {kind:'activity_start', raw:'9:00 开始测试', value:null, precision:'text',
     derived:false, derivation:'', conflict:true}],
  open_questions:['开始时间只写“9:00”，未写明日期'],
  evidence:[{ref:'p4', quote:'2026年10月18日 8:30 在紫金港校区游泳馆集合', chunk:'chunk-4', locator:'第 5 行'}]};

const SOURCE = {platform:'ckc_public', record_id:'ckc:3212562',
  origin:'https://webplus.zju.edu.cn/_s597/2026/1010/c78853a3212562/page.psp',
  college:'竺可桢学院', title:'游泳通级测试', published_at:'2026-10-10', body_sha256:'b'.repeat(64),
  has_unread_content:true,
  attachments:[{ref:'att_1', name:'分组表.xlsx', parsed:false,
    url:'https://webplus.zju.edu.cn/_upload/article/files/ab/cd/1234/abc-123.xlsx'}]};

function analysis(overrides = {}) {
  return {extractor:'deepseek', model:'deepseek-flash', prompt_version:'notice-extract-v1',
    status:'ready', error:null, result_at:'2026-10-12T10:05:00+08:00', attempts:1,
    usage:{prompt_tokens:900, completion_tokens:320, total_tokens:1220, latency_ms:850},
    body_sha256:'a'.repeat(64),
    payload:{notice_summary:'报名与两批测试安排', coverage:'full', coverage_note:'',
      events:[EVENT], open_questions:['附件《分组表.xlsx》未解析，分组需查看附件']},
    ...overrides};
}

function rulesAnalysis() {
  return {extractor:'rules', model:'', prompt_version:'notice-extract-v1', status:'ready',
    error:null, result_at:'2026-10-12T10:01:00+08:00', attempts:1, usage:{}, body_sha256:'a'.repeat(64),
    payload:{notice_summary:'', coverage:'partial', events:[],
      coverage_note:'离线规则提取：按句子与关键词生成候选事项。',
      open_questions:['附件《分组表.xlsx》未下载、未解析，可能包含时间、地点或名单']}};
}

function payload(model = {configured:true}, documents) {
  return {model: Object.assign({provider:'deepseek', provider_label:'DeepSeek', model:'deepseek-flash',
      prompt_version:'notice-extract-v1', consent_required:true,
      sends:'通知标题、正文段落、发布日期、学院名称、平台记录 ID 与附件名称'}, model),
    documents: documents === undefined ? [{document:'doc-1', name:'<script>游泳通级测试</script>.txt',
      status:'ready', created:'2026-10-12T10:00:00+08:00', body_sha256:'a'.repeat(64),
      source:SOURCE, analyses:[rulesAnalysis(), analysis()]}] : documents};
}

function harness(responder) {
  const nodes = new Map(), calls = [], timers = [], opened = [];
  const get = id => {if (!nodes.has(id)) nodes.set(id, element('div')); return nodes.get(id);};
  const ctx = vm.createContext({$:get, el:element, sessionEpoch:0, loggedIn:true,
    TIME_KIND_LABELS:{registration_deadline:'报名/提交截止', activity_start:'开始时间',
      activity_end:'结束时间', assembly:'集合/签到', other:'其他时间'},
    TIME_PRECISION_LABELS:{datetime:'精确到分', date:'只到日', month:'只到月', text:'仅原文表述'},
    ANALYSIS_STATUS_LABELS:{queued:'等待整理', running:'正在整理', ready:'已整理',
      partial:'部分结果', failed:'整理失败'},
    date:value=>value, toast(){}, refresh:async()=>{},
    openSource:(...args)=>opened.push(args),
    setTimeout:(fn,ms)=>{timers.push({fn,ms});return timers.length;}, clearTimeout(){},
    api:(path,options)=>{calls.push({path,options});return responder(path,options);}});
  vm.runInContext(fs.readFileSync('campus_agent/static/notice-analysis.js','utf8'),ctx);
  return {ctx,get,calls,timers,opened,run:code=>vm.runInContext(code,ctx),
    text:node=>{const seen=[];const walk=n=>{if(typeof n==='string'){seen.push(n);return;}
      if(!n)return;if(n.textContent)seen.push(n.textContent);(n.children||[]).forEach(walk);};
      walk(node);return seen.join('\n');}};
}

function texts(node, predicate) {
  const found = [];
  const walk = n => {if (!n || typeof n !== 'object') return;
    if (predicate(n)) found.push(n);
    (n.children || []).forEach(walk);};
  walk(node);
  return found;
}

test('source card keeps official provenance and marks attachments unparsed', async()=>{
  const h = harness(async()=>payload());
  await h.run('loadNoticeAnalyses()');
  const card = h.get('notice-analysis-list').children[0];
  // Model and server text is rendered as text only.
  assert.equal(card.children[0].textContent, '<script>游泳通级测试</script>.txt');
  const links = texts(card, n=>n.tag==='a');
  const origin = links.find(a=>a.textContent.startsWith('查看官方原文'));
  assert.equal(origin.href, SOURCE.origin);
  assert.equal(origin.rel, 'noopener noreferrer');
  assert.equal(origin.target, '_blank');
  const attachment = links.find(a=>a.href===SOURCE.attachments[0].url);
  assert.equal(attachment.textContent, '分组表.xlsx ↗');
  const body = h.text(card);
  assert.match(body, /均未下载、未解析/);
  assert.match(body, /记录 ckc:3212562/);
  assert.match(body, /正文版本 bbbbbbbbbbbb/);
  // Attachment refs are shown so a user can match them against model output.
  assert.match(body, /att_1/);
});

test('event card separates deadline, assembly and start, and links evidence to the chunk', async()=>{
  const h = harness(async()=>payload());
  await h.run('loadNoticeAnalyses()');
  const card = h.get('notice-analysis-list').children[0];
  const event = texts(card, n=>n.className==='notice-event')[0];
  assert.equal(event.children[0].textContent, '<b>第一批测试</b>');
  const body = h.text(event);
  assert.match(body, /报名\/提交截止/);
  assert.match(body, /集合\/签到/);
  assert.match(body, /开始时间/);
  assert.match(body, /原文：2026年10月15日 17:00 · 精确到分/);
  assert.match(body, /推导：原文 17:00 直接规范化（待核对）/);
  assert.match(body, /与其他时间矛盾，未自动消解/);
  assert.match(body, /时间待核对/);
  assert.match(body, /地点：紫金港校区游泳馆/);
  assert.match(body, /关联附件：分组表.xlsx（未解析）/);
  assert.match(body, /待核对：开始时间只写“9:00”，未写明日期/);
  assert.match(body, /引用已定位，语义仍需人工核对/);
  const citations = texts(event, n=>n.className==='citation');
  assert.equal(citations.length, 1);
  assert.match(citations[0].children[1].textContent, /2026年10月18日 8:30 在紫金港校区游泳馆集合/);
  citations[0].onclick();
  assert.deepEqual(h.opened[0], ['doc-1','chunk-4']);
  assert.match(h.text(card), /tokens 1220 · 850 ms/);
});

test('model analysis needs explicit consent before any request is sent', async()=>{
  const h = harness(async(path)=>path==='/api/notice-analyses' ? payload() : {ok:true});
  await h.run('loadNoticeAnalyses()');
  const card = h.get('notice-analysis-list').children[0];
  const button = texts(card, n=>n.tag==='button'&&n.textContent==='用模型整理事件')[0];
  assert.equal(button.disabled, true);
  assert.equal(h.calls.filter(c=>c.path.includes('/analysis')).length, 0);
  assert.match(h.text(card), /勾选上方同意项后/);
  h.get('notice-model-consent').checked = true;
  h.get('notice-model-consent').onchange();
  const enabled = texts(h.get('notice-analysis-list').children[0],
    n=>n.tag==='button'&&n.textContent==='用模型整理事件')[0];
  assert.equal(enabled.disabled, false);
  assert.match(h.text(h.get('notice-analysis-list').children[0]), /将把通知标题、正文段落与附件名称发送给 DeepSeek/);
  await enabled.onclick();
  const call = h.calls.find(c=>c.path==='/api/documents/doc-1/analysis');
  assert.equal(call.options.method, 'POST');
  assert.equal(JSON.stringify(call.options.json), JSON.stringify({mode:'model', consent:true}));
});

test('offline rules stay available and never claim a model result', async()=>{
  const h = harness(async(path)=>path==='/api/notice-analyses' ? payload() : {ok:true});
  await h.run('loadNoticeAnalyses()');
  const card = h.get('notice-analysis-list').children[0];
  const rules = texts(card, n=>n.tag==='button'&&n.textContent==='离线重新提取')[0];
  assert.equal(rules.disabled, false);
  await rules.onclick();
  const call = h.calls.find(c=>c.path==='/api/documents/doc-1/analysis');
  assert.equal(JSON.stringify(call.options.json), JSON.stringify({mode:'rules'}));
  const body = h.text(card);
  assert.match(body, /离线规则提取/);
  assert.match(body, /离线规则提取：按句子与关键词生成候选事项。/);
  assert.match(body, /附件《分组表.xlsx》未下载、未解析/);
});

test('an unconfigured provider hides the model button and keeps the consent box off', async()=>{
  const h = harness(async()=>payload({configured:false, model:'', provider_label:'DeepSeek'}));
  await h.run('loadNoticeAnalyses()');
  const card = h.get('notice-analysis-list').children[0];
  assert.equal(texts(card, n=>n.tag==='button'&&n.textContent==='用模型整理事件').length, 0);
  assert.equal(h.get('notice-model-consent').disabled, true);
  assert.equal(h.get('notice-model-consent').checked, false);
  assert.match(h.get('notice-model-provider').textContent, /未配置模型/);
  assert.match(h.get('notice-analysis-status').textContent, /未配置模型；仍可保存通知并使用离线规则提取/);
});

test('a failed run keeps the previous result visible and offers a retry', async()=>{
  const failed = analysis({status:'failed', attempts:2,
    error:{code:'RATE_LIMITED', message:'模型提供方限流，请稍后重试'}});
  const h = harness(async()=>payload(undefined, [{document:'doc-1', name:'游泳通级测试.txt',
    status:'ready', created:'2026-10-12T10:00:00+08:00', body_sha256:'a'.repeat(64),
    source:SOURCE, analyses:[rulesAnalysis(), failed]}]));
  await h.run('loadNoticeAnalyses()');
  const body = h.text(h.get('notice-analysis-list').children[0]);
  assert.match(body, /整理失败/);
  assert.match(body, /模型提供方限流，请稍后重试（RATE_LIMITED）/);
  assert.match(body, /本次整理失败，下方仍显示上次成功的结果/);
  // The last successful events are still rendered for review.
  assert.equal(texts(h.get('notice-analysis-list').children[0], n=>n.className==='notice-event').length, 1);
  assert.doesNotMatch(body, /test-secret|Bearer/);
});

test('a running analysis polls again and blocks a second run', async()=>{
  const h = harness(async()=>payload(undefined, [{document:'doc-1', name:'游泳通级测试.txt',
    status:'ready', created:'2026-10-12T10:00:00+08:00', body_sha256:'a'.repeat(64),
    source:SOURCE, analyses:[analysis({status:'running', payload:{}, result_at:null})]}]));
  await h.run('loadNoticeAnalyses()');
  const card = h.get('notice-analysis-list').children[0];
  assert.match(h.text(card), /正在整理，请稍候/);
  assert.equal(h.timers.length, 1);
  assert.equal(h.timers[0].ms, 2000);
  for (const button of texts(card, n=>n.tag==='button')) {
    if (button.textContent.includes('重新提取') || button.textContent.includes('用模型整理')) {
      assert.equal(button.disabled, true);
    }
  }
});

test('account switch clears the panel and ignores a delayed response', async()=>{
  let resolve;
  const h = harness(()=>new Promise(r=>resolve=r));
  const pending = h.run('loadNoticeAnalyses()');
  h.run('sessionEpoch++; clearNoticeAnalyses();');
  resolve(payload());
  await pending;
  assert.equal(h.get('notice-analysis-list').children.length, 0);
  assert.equal(h.get('notice-model-consent').checked, false);
  assert.equal(h.get('notice-model-consent').disabled, true);
  assert.doesNotMatch(h.get('notice-analysis-status').textContent, /DeepSeek/);
});

test('a partial result explains which paragraphs were not analysed', async()=>{
  const partial = analysis({status:'partial', payload:{notice_summary:'', coverage:'partial',
    coverage_note:'仅纳入 p1–p40；p41–p120 未送模型，后半部分要求需人工核对原文',
    events:[EVENT], open_questions:[]}});
  const h = harness(async()=>payload(undefined, [{document:'doc-1', name:'长通知.txt',
    status:'ready', created:'2026-10-12T10:00:00+08:00', body_sha256:'a'.repeat(64),
    source:null, analyses:[partial]}]));
  await h.run('loadNoticeAnalyses()');
  const body = h.text(h.get('notice-analysis-list').children[0]);
  assert.match(body, /部分结果/);
  assert.match(body, /未送模型/);
  assert.match(body, /手动上传的资料：没有官方来源记录/);
});

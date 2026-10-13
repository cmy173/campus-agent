"use strict";
const $ = id => document.getElementById(id);
let csrf = '', loggedIn = false, authMode = 'passport', accountMode='offline', taskFilter = 'pending', documents = [], tasks = [], timer, sessionEpoch = 0;
const titles = {home:'校园助手',library:'我的资料',tasks:'待办与截止',activities:'活动探索',settings:'隐私与设置',campus:'我的学业'};
const TIME_KIND_LABELS = {registration_deadline:'报名/提交截止',activity_start:'开始时间',activity_end:'结束时间',assembly:'集合/签到',other:'其他时间'};
const TIME_PRECISION_LABELS = {datetime:'精确到分',date:'只到日',month:'只到月',text:'仅原文表述'};
const ANALYSIS_STATUS_LABELS = {queued:'等待整理',running:'正在整理',ready:'已整理',partial:'部分结果',failed:'整理失败'};
function el(tag, text, className) { const node = document.createElement(tag); if(text !== undefined) node.textContent = text; if(className) node.className = className; return node; }
function toast(message) { $('toast').textContent = message; $('toast').classList.remove('hidden'); clearTimeout(timer); timer = setTimeout(()=>$('toast').classList.add('hidden'),5000); }
async function api(path, options={}) {
  const headers = {'X-Campus-Request':'1', 'X-CSRF-Token':csrf, ...options.headers};
  if(options.json !== undefined) { headers['Content-Type']='application/json'; options.body=JSON.stringify(options.json); delete options.json; }
  const res=await fetch(path,{...options,headers});
  const data=await res.json();
  if(!res.ok) { if(res.status===401 && !path.startsWith('/api/auth')) showAuth(); const error=new Error(typeof data.detail==='string'?data.detail:(data.detail?.message||'操作失败，请重试')); if(data.detail&&typeof data.detail==='object')error.detail=data.detail; throw error; }
  return data;
}
function resetMessages(){const box=el('div',undefined,'welcome');box.append(el('span','✦'));const p=el('p','你好，我是你的校园资料助手。');p.append(el('br'),el('small','上传资料后，试着询问一个具体问题。也可以先导入演示资料体验。'));const b=el('button','体验演示资料 ↗','secondary');b.id='demo-home';b.onclick=importDemo;box.append(p,b);$('messages').replaceChildren(box);}
function clearPrivateUI(){sessionEpoch++;if(typeof clearCollegeNotices==='function')clearCollegeNotices();if(typeof clearNoticeAnalyses==='function')clearNoticeAnalyses();if(typeof clearCampusUI==='function')clearCampusUI();documents=[];tasks=[];for(const id of ['messages','documents','tasks','audit','source-body'])$(id).replaceChildren();resetMessages();$('source-dialog').close();$('use-model').checked=false;$('mode-label').replaceChildren(el('i'),document.createTextNode('本地检索模式'));$('account-name').textContent='尚未登录';$('nav-count').textContent='0';$('task-count').textContent='0';for(const id of ['stat-docs','stat-tasks','stat-due'])$(id).textContent='0';}
function showAuth(){loggedIn=false;csrf='';clearPrivateUI();renderAccountMode();showPassportAuth();}
function date(value){ return value?new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(value)):'时间待核对'; }
function showView(view){document.querySelectorAll('.view').forEach(n=>n.classList.add('hidden'));$('view-'+view).classList.remove('hidden');document.querySelectorAll('.nav').forEach(n=>n.classList.toggle('active',n.dataset.view===view));$('breadcrumb').textContent='工作台 / '+titles[view];if(view==='activities')loadActivities().catch(e=>toast(e.message));if(view==='settings')loadSettings().catch(e=>toast(e.message));if(view==='campus')loadCampus().catch(e=>toast(e.message));if(view==='tasks'){if(typeof loadCollegeNotices==='function')loadCollegeNotices();if(typeof loadNoticeAnalyses==='function')loadNoticeAnalyses().catch(e=>toast(e.message));}}
document.querySelectorAll('[data-view]').forEach(n=>n.addEventListener('click',()=>showView(n.dataset.view)));
$('today').textContent=new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'long',day:'numeric',weekday:'short'}).format(new Date());
$('auth-dialog').addEventListener('cancel',e=>{e.preventDefault();cancelPassportAuth();});
async function signedIn(me){clearPrivateUI();csrf=me.csrf;loggedIn=true;accountMode=me.mode||'legacy';$('account-name').textContent=me.username;$('avatar').textContent=me.username[0].toUpperCase();$('auth-dialog').close();renderAccountMode();showView(accountMode==='legacy'?'home':'campus');await refresh();await loadSettings();if(me.campus)await automaticCampusSync(me.campus);}
$('logout').onclick=async()=>{try{await api('/api/auth/logout',{method:'POST'});documents=[];tasks=[];$('messages').replaceChildren();$('documents').replaceChildren();$('tasks').replaceChildren();$('audit').replaceChildren();$('account-name').textContent='尚未登录';showAuth();}catch(e){toast(e.message);}};
async function refresh(){if(!loggedIn)return;const epoch=sessionEpoch;const results=await Promise.all([api('/api/documents'),api('/api/tasks')]);if(epoch!==sessionEpoch)return;[documents,tasks]=results;renderDocuments();if(!document.activeElement?.closest('.task-edit'))renderTasks();const pending=tasks.filter(t=>t.state==='pending');$('nav-count').textContent=documents.length;$('task-count').textContent=pending.length;$('stat-docs').replaceChildren(document.createTextNode(documents.filter(d=>d.status==='ready').length+' '),el('small','份'));$('stat-tasks').replaceChildren(document.createTextNode(pending.length+' '),el('small','项'));const now=Date.now();const soon=pending.filter(t=>t.due&&Date.parse(t.due)>=now&&Date.parse(t.due)<=now+7*864e5);$('stat-due').replaceChildren(document.createTextNode(soon.length+' '),el('small','项'));}
function renderDocuments(){const root=$('documents');root.replaceChildren();if(!documents.length){root.append(el('div','还没有资料。上传第一份通知，或导入演示资料开始体验。','empty'));return;}const labels={queued:'等待解析',processing:'正在解析',ready:'已整理',failed:'解析失败'};for(const d of documents){const row=el('article',undefined,'document-row');row.append(el('span',d.name.split('.').pop().toUpperCase(),'file-type'));const info=el('div',undefined,'doc-info');info.append(el('strong',d.name),el('small',(d.kind==='notice'?'通知 · 自动提取待办':'课程 / 培养方案')+' · '+date(d.created)));if(d.error)info.append(el('small',d.error,'error'));for(const warning of d.warnings)info.append(el('small',warning));row.append(info);const actions=el('div',undefined,'row-actions');actions.append(el('span',labels[d.status],'badge '+(d.status==='failed'?'red':d.status==='ready'?'':'amber')));const view=el('button','查看原文','text-button');view.onclick=()=>openSource(d.id);const del=el('button','删除','text-button');del.onclick=async()=>{if(!confirm('删除「'+d.name+'」及其提取文字和待办？此操作不可撤销。'))return;try{await api('/api/documents/'+d.id,{method:'DELETE'});$('messages').replaceChildren();await refresh();toast('资料及派生内容已删除');}catch(e){toast(e.message);}};actions.append(view,del);row.append(actions);root.append(row);}}
async function openSource(id,chunk){const epoch=sessionEpoch;try{const d=await api('/api/documents/'+id);if(epoch!==sessionEpoch)return;$('source-title').textContent=d.name;const root=$('source-body');root.replaceChildren();const a=el('a','下载原文件','text-button');a.href='/api/documents/'+id+'/download';a.download=d.name;root.append(a);if(d.error)root.append(el('p',d.error,'error'));for(const w of d.warnings)root.append(el('p',w,'footnote'));if(!d.chunks.length)root.append(el('p','尚无可展示的提取文字。','empty'));let selected;for(const c of d.chunks){const section=el('section',undefined,'source-chunk'+(c.id===chunk?' highlight':''));section.append(el('strong',(c.ref?c.ref+' · ':'')+c.locator),el('pre',c.text));root.append(section);if(c.id===chunk)selected=section;}if(!$('source-dialog').open)$('source-dialog').showModal();if(selected)selected.scrollIntoView({block:'center'});}catch(e){toast(e.message);}}
$('close-source').onclick=()=>$('source-dialog').close();
$('upload-form').onsubmit=async e=>{e.preventDefault();const file=$('upload-file').files[0];if(!file)return;if(file.size>20*1024*1024){toast('文件不能超过 20 MB');return;}$('upload-button').disabled=true;$('upload-button').textContent='上传中…';try{const result=await upload(file.name,file,$('document-kind').value);toast(result.duplicate?'该资料已存在，未重复导入':'已上传，正在解析');$('upload-file').value='';await refresh();}catch(err){toast(err.message);}finally{$('upload-button').disabled=false;$('upload-button').textContent='上传并整理';}};
function upload(name,body,kind){return api('/api/documents',{method:'POST',headers:{'Content-Type':'application/octet-stream','X-Filename':encodeURIComponent(name),'X-Document-Kind':kind},body});}
async function importDemo(){const buttons=[$('demo-home'),$('demo-library')];buttons.forEach(b=>{if(b)b.disabled=true;});try{const future=new Date(Date.now()+3*864e5);const day=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(future);await upload('演示通知.txt',new Blob(['校园通知（虚构演示，不是学校真实通知）\n实验报告提交截止：'+day+' 18:00；提交地点：课程平台。\n志愿活动报名截止：下周五，具体时间待核对。'],{type:'text/plain'}),'notice');await upload('演示课程资料.txt',new Blob(['数据结构课程资料（虚构演示）\n二叉搜索树：每个节点左子树中的值小于该节点，右子树中的值大于该节点。\n课程报告要求：说明算法思路、时间复杂度，并提供测试用例。'],{type:'text/plain'}),'material');toast('演示资料已导入，解析完成后即可提问');await refresh();}catch(e){toast(e.message);}finally{buttons.forEach(b=>{if(b)b.disabled=false;});}}
$('demo-home').onclick=importDemo;$('demo-library').onclick=importDemo;
function renderTasks(){
  const root=$('tasks');root.replaceChildren();
  const list=tasks.filter(t=>taskFilter==='all'||t.state===taskFilter);
  const empty={done:'还没有已完成事项。',ignored:'没有已忽略的事项。',
    pending:'当前没有待办。将通知作为“通知”类型上传后，系统会提取候选事项。'};
  if(!list.length){root.append(el('div',empty[taskFilter]||empty.pending,'empty'));return;}
  for(const t of list){
    const card=el('article',undefined,'task-card');
    const meta=el('div',undefined,'task-meta');
    const overdue=t.due&&Date.parse(t.due)<Date.now()&&t.state!=='done';
    const settled={done:'已完成',ignored:'已忽略'}[t.state];
    meta.append(el('span',t.reviewed?'已核对':'待核对','badge '+(t.reviewed?'':'amber')),
      el('span',t.origin==='model'?'模型整理':'规则提取','badge'),
      el('span',settled||(overdue?'已过期 · '+date(t.due):date(t.due)),settled?'':(overdue?'error':'')));
    if(t.time_kind)meta.append(el('span',TIME_KIND_LABELS[t.time_kind]||t.time_kind,'badge amber'));
    const source=el('button',t.name+' · '+t.locator+' ↗','text-button');
    source.onclick=()=>openSource(t.document,t.chunk);
    meta.append(source);
    card.append(meta,el('h3',t.title));
    if(t.raw_date)card.append(el('p','原文时间：'+t.raw_date+(t.time_precision?' · '+(TIME_PRECISION_LABELS[t.time_precision]||t.time_precision):''),'footnote'));
    if(!t.due&&t.raw_date)card.append(el('p',t.needs_attachment
      ?'缺少完整时间；这条通知有未解析附件，细节可能在附件中，请核对附件。'
      :'原文没有给出年份或时刻，未推算截止时间。','footnote'));
    if(t.derived_note)card.append(el('p','推导依据：'+t.derived_note+'（仍待核对）','footnote'));
    if(t.location)card.append(el('p','地点：'+t.location));
    else if(t.needs_attachment)card.append(el('p','地点：需查看附件，原文未给出','footnote'));
    if((t.attachment_refs||[]).length)card.append(el('p','关联附件 '+(t.attachment_refs||[]).length+' 个，均未下载、未解析','footnote'));
    for(const evidence of t.evidence||[]){
      const button=el('button',undefined,'citation');
      button.append(el('strong',(evidence.ref||'原文')+' ↗'),el('span',evidence.quote));
      button.onclick=()=>openSource(t.document,evidence.chunk||t.chunk);
      card.append(button);
    }
    if(t.origin==='model')card.append(el('p','以上引用已定位到原文；语义是否正确仍需人工核对。','footnote'));
    const form=el('form',undefined,'task-edit');
    const label=el('label','核对截止时间');
    const input=el('input');input.type='datetime-local';input.setAttribute('aria-label','核对截止时间');
    if(t.due)input.value=new Date(Date.parse(t.due)+8*3600e3).toISOString().slice(0,16);
    label.append(input);
    const state=el('select');state.setAttribute('aria-label','待办状态');
    for(const [v,s] of [['pending','未完成'],['done','已完成'],['ignored','忽略']]){const o=el('option',s);o.value=v;state.append(o);}
    state.value=t.state;
    const button=el('button','确认保存','secondary');button.type='submit';
    form.append(label,state,button);
    form.onsubmit=async e=>{e.preventDefault();button.disabled=true;try{await api('/api/tasks/'+t.id,{method:'PATCH',json:{state:state.value,due:input.value?input.value+':00+08:00':null}});await refresh();toast('待办已保存');}catch(err){toast(err.message);button.disabled=false;}};
    card.append(form);
    root.append(card);
  }
}
document.querySelectorAll('[data-task-filter]').forEach(button=>button.onclick=()=>{taskFilter=button.dataset.taskFilter;document.querySelectorAll('[data-task-filter]').forEach(b=>b.classList.toggle('active',b===button));renderTasks();});
function cite(c){const button=el('button',undefined,'citation');button.append(el('strong',(c.label?c.label+' ':'')+(c.name||'资料')+' · '+c.locator+' ↗'),el('span',c.text));button.onclick=()=>openSource(c.document,c.id||c.chunk);return button;}
async function ask(query){if(!query.trim())return;const epoch=sessionEpoch;showView('home');const root=$('messages');root.querySelector('.welcome')?.remove();root.append(el('div',query,'message user'));const pending=el('div',$('use-model').checked?'检索资料，等待模型回答…':'正在查找当前账号资料…','pending-message');root.append(pending);root.scrollTop=root.scrollHeight;$('send').disabled=true;try{const r=await api('/api/chat',{method:'POST',json:{query,use_model:$('use-model').checked}});if(epoch!==sessionEpoch)return;pending.remove();const msg=el('div',undefined,'message assistant');msg.append(el('div',(r.mode==='openai'?'OpenAI · ':'本地检索 · ')+r.steps.join(' → '),'trace'),el('div',r.answer));if(r.warning)msg.append(el('p',r.warning,'error'));for(const c of r.citations||[])msg.append(cite(c));for(const t of r.tasks||[]){const b=el('button',undefined,'citation');b.append(el('strong',date(t.due)+' · '+(t.reviewed?'已核对':'待核对')),el('span',t.title));b.onclick=()=>openSource(t.document,t.chunk);msg.append(b);}root.append(msg);root.scrollTop=root.scrollHeight;}catch(e){pending.textContent=e.message;pending.classList.add('error');}finally{$('send').disabled=false;}}
$('chat-form').onsubmit=e=>{e.preventDefault();const q=$('query').value;$('query').value='';ask(q);};
$('query').onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();if(!$('send').disabled)$('chat-form').requestSubmit();}};
document.querySelectorAll('[data-query]').forEach(b=>b.onclick=()=>ask(b.dataset.query));
$('clear-chat').onclick=resetMessages;
async function loadActivities(){const r=await api('/api/activities?target='+encodeURIComponent($('target').value));const root=$('activities');root.replaceChildren(el('div','预计演示学分 '+r.credits+' · 剩余目标 '+r.remaining,'activity-summary'));for(const a of r.activities){const card=el('article',undefined,'activity-card');card.append(el('h3',a.title),el('span',a.credits+' 演示学分','badge'),el('p',date(a.start)+' – '+date(a.end)),el('p','报名截止 '+date(a.deadline)+' · 演示名额 '+a.seats));root.append(card);}if(!r.activities.length)root.append(el('div','无需推荐活动，或没有符合条件的演示活动。','empty'));root.append(el('p',r.reason+' '+r.caveat,'footnote'));}
$('activity-form').onsubmit=e=>{e.preventDefault();loadActivities().catch(err=>toast(err.message));};
async function loadSettings(){if(!loggedIn)return;const epoch=sessionEpoch;const [model,audit]=await Promise.all([api('/api/model'),api('/api/audit')]);if(epoch!==sessionEpoch)return;$('use-model').disabled=!model.configured;$('model-hint').textContent=model.configured?'已配置 · '+model.model:'未配置';$('model-status').textContent=model.configured?'已配置：'+model.model+'。可在对话中按需启用。':'尚未配置 OpenAI，所有问答使用本地检索。';const notice=model.notice||{};$('notice-model-status').textContent=notice.configured?'已配置：'+notice.provider_label+' · '+notice.model+'（提示词 '+notice.prompt_version+'）。在”待办与截止 → 通知整理”勾选同意后，才会把通知内容发送给该提供方。':'尚未配置通知整理模型；通知仍可保存并使用离线规则提取。在启动服务的终端设置 DEEPSEEK_API_KEY 后重启即可启用。';// Show/hide API key input section based on configuration status
const apiKeySection=$('notice-api-key-section');if(apiKeySection){apiKeySection.classList.toggle('hidden',notice.configured);}// Clear the input field if showing
if(apiKeySection&&!notice.configured){$('notice-api-key-input').value='';}if(typeof loadWebvpnStatus==='function')loadWebvpnStatus();const root=$('audit');root.replaceChildren();for(const a of audit){const row=el('div',undefined,'audit-row');row.append(el('span',a.action+' · '+a.result),el('span',date(a.created)));root.append(row);}if(!audit.length)root.append(el('p','暂无操作记录','footnote'));}
$('use-model').onchange=()=>{$('mode-label').replaceChildren(el('i'),document.createTextNode($('use-model').checked?'OpenAI 辅助问答':'本地检索模式'));};
// Notice API Key configuration handlers
if($('notice-api-key-save')){
  $('notice-api-key-save').onclick=async()=>{
    const key=$('notice-api-key-input').value.trim();
    if(!key){toast('请输入 API Key');return;}
    try{
      await api('/api/model/configure',{method:'POST',json:{key_type:'deepseek',key_value:key}});
      $('notice-api-key-input').value='';
      toast('API Key 已保存（仅本次会话有效）');
      await loadSettings();
    }catch(e){toast(e.message);}
  };
}
if($('notice-api-key-clear')){
  $('notice-api-key-clear').onclick=async()=>{
    try{
      await api('/api/model/configure',{method:'POST',json:{key_type:'deepseek',key_value:''}});
      toast('API Key 已清除');
      await loadSettings();
    }catch(e){toast(e.message);}
  };
}
$('clear-data').onclick=async()=>{if(!confirm('永久清除当前账号的全部资料、提取文字、待办、通知整理结果、学业信息、校园连接和操作记录？本地账号会保留。'))return;try{await api('/api/data',{method:'DELETE'});clearCampusUI();if(typeof clearCollegeNotices==='function')clearCollegeNotices();if(typeof clearNoticeAnalyses==='function')clearNoticeAnalyses();$('messages').replaceChildren();$('source-dialog').close();await refresh();await loadSettings();toast('资料已清除');}catch(e){toast(e.message);}};
setInterval(()=>{if(loggedIn&&documents.some(d=>['queued','processing'].includes(d.status))){refresh().catch(e=>toast(e.message));if(typeof loadNoticeAnalyses==='function')loadNoticeAnalyses().catch(()=>{});}},1800);

$('logout-settings').onclick=()=> $('logout').click();


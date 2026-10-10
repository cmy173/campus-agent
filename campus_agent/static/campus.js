"use strict";
let campusFlow = '', campusBusy = false, campusCanResume = false;
const campusStages={terms:'学期列表',grades:'成绩',timetable:'课表',service_login:'教务会话',profile:'基本信息',profile_eta:'ETA 基本信息',profile_zdbk:'教务基本信息'};
function campusFailureText(failure) {
  const prefix=campusStages[failure.stage];
  const message=failure.message||'查询失败';
  let text=(prefix&&!message.startsWith(prefix+'：')?prefix+'：':'')+message;
  const d=failure.diagnostic;
  if(d)text+='（'+[d.stage,d.method,d.reason,d.elapsed_ms==null?'':(d.elapsed_ms/1000).toFixed(1)+' 秒'].filter(Boolean).join(' · ')+ '）';
  return text;
}
function campusDiagnostics(root, failures, attemptedAt, connectorRevision, syncRevision) {
  if(!failures.length)return;
  const details=el('details'),summary=el('summary','同步诊断（不含账号或 Cookie）');
  // Only explicitly safe metadata produced by the connector, never the snapshot or upstream body.
  const diagnostics=failures.map(f=>{
    const d=f.diagnostic||{},result={code:f.code,stage:f.stage||d.stage};
    for(const key of ['method','host','path','reason','http_status','limit_bytes','observed_bytes','content_kind','elapsed_ms','semester_codes','option_shapes','data_request','field_status','schema_shape','form_attempts','upstream_flags','response_shape','query_semester','query_year'])if(d[key]!=null)result[key]=d[key];
    return result;
  });
  const diagnosticText=JSON.stringify({connector_revision:connectorRevision||null,sync_revision:syncRevision||null,
    attempted_at:attemptedAt||null,failures:diagnostics},null,2);
  const copy=el('button','复制诊断','secondary');copy.type='button';
  copy.onclick=async()=>{
    try{await navigator.clipboard.writeText(diagnosticText);toast('诊断已复制，可直接粘贴到聊天中');}
    catch{toast('浏览器未允许复制，请选中下方诊断文字后按 Ctrl+C');}
  };
  details.append(summary,copy,el('pre',diagnosticText));
  root.append(details);
}
function clearCampusUI() {
  campusPassportReady=false;
  campusFlow='';campusBusy=false;campusCanResume=false;
  for(const id of ['campus-prepare','campus-login-button','campus-disconnect','campus-refresh-captcha']) $(id).disabled=false;
  for(const id of ['campus-username','campus-password','campus-authcode','campus-captcha']) $(id).value='';
  for(const id of ['campus-profile','campus-courses','campus-grades','campus-warnings']) $(id).replaceChildren();
  for(const id of ['campus-cas-image','campus-table-image']) $(id).removeAttribute('src');
  for(const id of ['campus-login','campus-cas-code','campus-table-code']) $(id).classList.add('hidden');
  $('campus-consent').checked=false;
  $('campus-status').textContent='尚未连接';
  $('campus-prepare').textContent='登录通行证并更新';
  $('campus-sync-button').disabled=true;
  $('campus-message').textContent='通行证登录时自动获取信息；离线时使用已有缓存。';
  if(typeof renderAccountMode==='function'){if(accountMode!=='legacy')accountMode='offline';renderAccountMode();}
  if(typeof resetAcademicViews==='function')resetAcademicViews();
}
function campusTable(root, headers, rows) {
  const wrap=el('div',undefined,'campus-table-wrap');
  const table=el('table',undefined,'campus-table');
  const head=el('thead'),hr=el('tr');
  headers.forEach(h=>{const th=el('th',h);th.scope='col';hr.append(th);});head.append(hr);
  const body=el('tbody');
  rows.forEach(row=>{const tr=el('tr');row.forEach(v=>tr.append(el('td',v===''||v==null?'未提供':String(v))));body.append(tr);});
  table.append(head,body);wrap.append(table);root.append(wrap);
}
function renderCampus(result) {
  campusPassportReady=Boolean(result.passport_connected);
  $('campus-prepare').textContent=campusPassportReady?'更新教务连接':'登录通行证并更新';
  if(accountMode!=='legacy'){accountMode=campusPassportReady?'passport':'offline';renderAccountMode();}
  $('campus-status').textContent=result.connected?'已连接本科教务网':'尚未连接 / 会话已失效';
  $('campus-status').classList.toggle('amber',!result.connected);
  $('campus-sync-button').disabled=!result.connected||campusBusy;
  campusCanResume=result.can_resume;
  $('campus-sync-button').textContent=campusCanResume?'继续获取未完成学期':'重新获取信息';
  const s=result.snapshot,warnings=s.warnings||[];
  const root=$('campus-warnings');root.replaceChildren();
  if(s.attempted_at&&result.connector_revision&&s.sync_revision!==result.connector_revision)
    root.append(el('p','当前显示旧版本同步记录；重新登录更新后会生成新诊断。','footnote'));
  const rangeFallback=warnings.some(w=>w.code==='TERM_RANGE_FALLBACK');
  warnings.filter(w=>!(rangeFallback&&w.stage==='terms'&& !['TERM_RANGE_FALLBACK','CACHED_TERM_RANGE'].includes(w.code))).forEach(w=>root.append(el('p',(w.term?w.term+'：':'')+campusFailureText(w),['TERM_RANGE_FALLBACK','PARTIAL_TIMETABLE','UNSUPPORTED_TERM_OPTIONS','PROFILE_PARTIAL','CACHED_TERM_RANGE','EMPTY_TIMETABLE_CACHE'].includes(w.code)?'footnote':'error')));
  campusDiagnostics(root,warnings,s.attempted_at,result.connector_revision,s.sync_revision);
  if(s.sync_progress){
    const p=s.sync_progress;
    root.append(el('p',`本轮课表已更新 ${p.completed} / ${p.total} 个可查询学期。`,'footnote'));
    if(p.pending.length)root.append(el('p','待查询：'+p.pending.map(t=>t.term).join('、'),'footnote'));
    if(p.failed?.length)root.append(el('p',p.failed.length+' 个学期查询失败或只取得部分信息；已有缓存仍可查看。','footnote'));
  }
  if(s.courses){
    const saved=s.courses.terms||[],known=s.courses.catalog||saved;
    root.append(el('p',`本机已保存 ${saved.length} 个学期的课表结果，其中 ${saved.filter(t=>t.data?.length).length} 个有课程；已知学期 ${known.length} 个。`,'footnote'));
  }
  const profile=$('campus-profile');profile.replaceChildren();
  if(s.profile){
    profile.append(el('p','获取时间：'+date(s.profile.fetched_at)+' · '+(s.profile.stale?'上次数据，本次未更新':'已获取的数据'),'footnote'));
    const p=s.profile.data;
    const fields=[['学号','student_id'],['姓名','name'],['学院','college'],['专业','major'],['班级','class_name'],['年级','year']];
    campusTable(profile,['字段','内容','来源 · 获取时间'],fields.map(([label,key])=>{
      const source=s.profile.field_sources?.[key];
      const checks=s.profile_checks||[];
      const missing=checks.some(c=>c.status==='read')?
        (checks.some(c=>c.status==='failed')?'已查询来源未提供；部分来源失败':'接口未返回可识别字段'):
        checks.some(c=>c.status==='failed')?'来源查询失败，见同步诊断':'尚未从接口取得';
      return [label,p[key],p[key]?(source?source.source+' · '+date(source.fetched_at)+(source.stale?' · 上次数据':''):'上次资料 · '+date(s.profile.fetched_at)):missing];
    }));
    if(fields.some(([,key])=>!p[key]))profile.append(el('p','已展示可用字段；缺失项保持空白，原因见各行和同步诊断。','footnote'));
  }else profile.append(el('p','尚未获取，或教务响应没有提供基本信息。','footnote'));
  if(typeof setAcademicSnapshot==='function')setAcademicSnapshot(s);
  $('campus-table-code').classList.toggle('hidden',!warnings.some(w=>w.code==='CAPTCHA_REQUIRED'));
}
async function loadCampus() {
  const epoch=sessionEpoch;
  const result=await api('/api/campus');
  if(epoch===sessionEpoch)renderCampus(result);
}
async function campusAction(action) {
  if(campusBusy)return;
  const epoch=sessionEpoch;campusBusy=true;
  const buttons=['campus-prepare','campus-login-button','campus-sync-button','campus-disconnect','campus-refresh-captcha'];
  buttons.forEach(id=>$(id).disabled=true);
  $('campus-message').textContent='正在连接校园服务，请稍候…';
  try{await action(epoch);}catch(err){if(epoch===sessionEpoch)$('campus-message').textContent=campusFailureText(err.detail||{message:err.message});}
  finally{if(epoch===sessionEpoch){campusBusy=false;buttons.forEach(id=>$(id).disabled=false);await loadCampus().catch(e=>toast(e.message));}}
}
$('campus-prepare').onclick=()=>campusAction(async epoch=>{
  if(!$('campus-consent').checked)throw new Error('请先同意读取本人的教务信息');
  campusFlow='';$('campus-password').value='';$('campus-login').classList.add('hidden');
  const r=await api('/api/campus/prepare',{method:'POST',json:{consent:true}});
  if(epoch!==sessionEpoch)return;
  campusFlow=r.flow_id;$('campus-authcode').value='';
  $('campus-cas-code').classList.toggle('hidden',!r.captcha);
  if(r.captcha)$('campus-cas-image').src=r.captcha;
  $('campus-login').classList.remove('hidden');$('campus-username').focus();
  $('campus-message').textContent='登录流程已准备，有效期 5 分钟。请输入校园账号与密码。';
});
$('campus-login').onsubmit=e=>{e.preventDefault();campusAction(async epoch=>{
  const body={flow_id:campusFlow,username:$('campus-username').value.trim(),password:$('campus-password').value,authcode:$('campus-authcode').value.trim(),consent:$('campus-consent').checked};
  $('campus-password').value='';campusFlow='';
  try{
    const r=await api('/api/campus/login',{method:'POST',json:body});
    if(epoch!==sessionEpoch)return;
    if(typeof resetAcademicViews==='function')resetAcademicViews();
    renderCampus(r);$('campus-message').textContent='本科教务网会话已验证，可以同步全部学期与成绩。';
  }finally{
    body.password='';if(epoch===sessionEpoch){$('campus-login').classList.add('hidden');$('campus-authcode').value='';$('campus-cas-image').removeAttribute('src');}
  }
});};
async function campusCaptcha(epoch) {
  const r=await api('/api/campus/captcha',{method:'POST'});
  if(epoch!==sessionEpoch)return;
  $('campus-table-image').src=r.captcha;$('campus-captcha').value='';$('campus-table-code').classList.remove('hidden');
}
$('campus-refresh-captcha').onclick=()=>campusAction(campusCaptcha);
$('campus-sync').onsubmit=e=>{e.preventDefault();campusAction(async epoch=>{
  let resume=campusCanResume,r;
  const captcha=$('campus-captcha').value.trim();$('campus-captcha').value='';
  do{
    r=await api('/api/campus/sync',{method:'POST',json:{resume,captcha}});
    if(epoch!==sessionEpoch)return;
    renderCampus(r);resume=true;
    const p=r.snapshot.sync_progress;
    $('campus-message').textContent=p?`正在同步全部学期：${p.completed} / ${p.total}…`:'正在同步…';
  }while(r.auto_continue);
  const warnings=r.snapshot.warnings||[];
  $('campus-message').textContent=warnings.some(w=>w.code==='TERM_RANGE_FALLBACK')?'已获取可用信息，学期范围使用降级查询；详情见下方。':warnings.length||r.snapshot.grades?.complete===false?'同步已结束，部分信息未更新，请查看下方说明。':'同步完成，已查询教务页面全部可选学期。';
  if(warnings.some(w=>w.code==='CAPTCHA_REQUIRED'))await campusCaptcha(epoch);
});};
$('campus-disconnect').onclick=()=>campusAction(async epoch=>{
  if(!confirm('断开校园会话并删除当前本地账号保存的校园基本信息、课表与成绩？'))return;
  await api('/api/campus',{method:'DELETE'});
  if(epoch===sessionEpoch){clearCampusUI();$('campus-message').textContent='校园会话和校园数据已清除。';}
});

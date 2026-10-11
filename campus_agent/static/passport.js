"use strict";
let passportFlow='',passportPreparation=0,passportLinkLegacy=false,campusPassportReady=false;
function renderAccountMode(){
  $('account-mode').textContent=accountMode==='passport'?'通行证会话 · 本机缓存':accountMode==='legacy'?'旧版资料账号':'离线缓存 · 获取时间见来源';
  let banner=$('account-session-banner');
  if(!banner){banner=el('div',undefined,'account-session-banner');banner.id='account-session-banner';document.querySelector('main').prepend(banner);}
  banner.replaceChildren();banner.classList.toggle('hidden',!loggedIn);
  if(loggedIn){banner.append(el('span',accountMode==='offline'?'正在查看此账号的离线缓存。校园会话需重新登录，缓存不代表最新教务信息。':accountMode==='legacy'?'正在查看旧版本地资料。可在设置中验证通行证并绑定，保留原有资料。':'通行证已验证；登录时自动获取信息，查询失败时保留原缓存。'));const b=el('button',accountMode==='legacy'?'绑定通行证':accountMode==='passport'?'更新信息':'登录更新','text-button');b.onclick=()=>accountMode==='passport'?campusAction(async epoch=>resumeCampusResult(await api('/api/campus/reconnect',{method:'POST'}),epoch)):showPassportAuth(accountMode==='legacy');banner.append(b);}
  $('auth-cache-back').classList.toggle('hidden',!loggedIn);
}
function passportFormMode(){
  const legacy=authMode!=='passport',register=authMode==='register';
  $('auth-title').textContent=register?'创建本地试用账号':legacy?'本地资料登录':passportLinkLegacy?'验证通行证并绑定当前资料':'登录浙大通行证';
  $('auth-description').textContent=legacy?'本地账号用于资料检索和演示，不验证校园身份。请设置独立密码，不要使用校园密码；以后可绑定通行证。':'登录后自动读取本人基本信息、课表和成绩。密码仅用于本次认证；本浏览器默认恢复已有账号的离线缓存，有效期 30 天。退出登录会关闭此入口。';
  $('auth-username-label').textContent=legacy?'本地用户名':'通行证账号';
  $('auth-password-label').textContent=legacy?'本地密码':'通行证密码';
  $('username').maxLength=legacy?32:128;$('password').minLength=legacy?10:1;
  $('auth-submit').textContent=register?'创建并试用':legacy?'打开本地资料':'登录并获取信息';
  $('auth-switch').textContent=legacy?'返回通行证登录':'本地资料登录';
  $('auth-refresh').classList.toggle('hidden',legacy);$('auth-captcha').classList.add('hidden');
  $('auth-cache-back').classList.toggle('hidden',!loggedIn);$('auth-error').textContent='';
}
function showPassportAuth(linkLegacy=false){
  // Several expired local API requests may arrive together. Prepare one browser-bound flow.
  if($('auth-dialog').open&&authMode==='passport'&&!linkLegacy&&(passportFlow||$('auth-submit').disabled))return;
  passportLinkLegacy=linkLegacy;authMode='passport';passportFlow='';$('password').value='';
  passportFormMode();if(!$('auth-dialog').open)$('auth-dialog').showModal();preparePassport();
}
async function preparePassport(){
  const serial=++passportPreparation;passportFlow='';$('auth-submit').disabled=true;$('auth-refresh').disabled=true;
  $('auth-error').textContent='正在准备通行证认证…';$('auth-captcha-image').removeAttribute('src');$('authcode').value='';
  try{const r=await api('/api/auth/passport/prepare',{method:'POST',json:{}});if(serial!==passportPreparation||authMode!=='passport')return;passportFlow=r.flow_id;$('auth-captcha').classList.toggle('hidden',!r.captcha);if(r.captcha)$('auth-captcha-image').src=r.captcha;$('auth-error').textContent='';}
  catch(e){if(serial===passportPreparation&&authMode==='passport')$('auth-error').textContent=typeof campusFailureText==='function'?campusFailureText(e.detail||{message:e.message}):e.message;}
  finally{if(serial===passportPreparation&&authMode==='passport'){$('auth-submit').disabled=!passportFlow;$('auth-refresh').disabled=false;}}
}
$('auth-switch').onclick=()=>{passportPreparation++;passportFlow='';$('password').value='';authMode=authMode==='passport'?'login':'passport';passportLinkLegacy=false;passportFormMode();$('auth-submit').disabled=false;if(authMode==='passport')preparePassport();};
$('auth-refresh').onclick=()=>preparePassport();
$('auth-cache-back').onclick=()=>{passportPreparation++;passportFlow='';$('password').value='';$('auth-captcha-image').removeAttribute('src');$('auth-dialog').close();};
$('auth-form').onsubmit=async e=>{
  e.preventDefault();const legacy=authMode!=='passport',register=authMode==='register';if(!legacy&&!passportFlow)return;
  const body={username:$('username').value.trim(),password:$('password').value};
  if(!legacy)Object.assign(body,{flow_id:passportFlow,authcode:$('authcode').value.trim(),link_legacy:passportLinkLegacy});
  $('password').value='';passportFlow='';$('auth-submit').disabled=true;$('auth-refresh').disabled=true;$('auth-switch').disabled=true;
  $('auth-error').textContent=legacy?'正在打开旧版资料…':'正在验证通行证并获取信息；请稍候…';
  try{const me=await api(legacy?'/api/auth/'+authMode:'/api/auth/passport/login',{method:'POST',json:body});$('authcode').value='';$('auth-captcha-image').removeAttribute('src');await signedIn(me);}
  catch(err){$('auth-error').textContent=typeof campusFailureText==='function'?campusFailureText(err.detail||{message:err.message}):err.message;}
  finally{body.password='';$('auth-submit').disabled=!legacy&&!passportFlow;$('auth-refresh').disabled=false;$('auth-switch').disabled=false;}
};
async function resumeCampusResult(result,epoch){
  let r=result;
  while(epoch===sessionEpoch){
    renderCampus(r);const p=r.snapshot.sync_progress;
    $('campus-message').textContent=p?'自动获取信息：'+p.completed+' / '+p.total+' 个学期…':'正在获取信息…';
    if(!r.auto_continue)break;
    r=await api('/api/campus/sync',{method:'POST',json:{resume:true}});
  }
  if(epoch!==sessionEpoch)return;
  const warnings=r.snapshot.warnings||[];
  $('campus-message').textContent=warnings.some(w=>w.code==='TERM_RANGE_FALLBACK')?'已自动获取可用信息，学期范围使用降级查询；详情见下方。':warnings.length?'自动获取已结束，部分信息未更新；已有缓存仍可查看。':'登录后已自动获取可查询学期与成绩。';
  if(warnings.length)$('campus-connection').open=true;
  if(warnings.some(w=>w.code==='CAPTCHA_REQUIRED'))await campusCaptcha(epoch);
}
async function automaticCampusSync(first){await campusAction(epoch=>resumeCampusResult(first,epoch));}
$('campus-prepare').onclick=()=>{
  if(campusPassportReady)return campusAction(async epoch=>resumeCampusResult(await api('/api/campus/reconnect',{method:'POST'}),epoch));
  showPassportAuth();
};
const bindingButton=el('button','将旧版资料绑定到通行证','secondary');bindingButton.id='passport-bind';bindingButton.onclick=()=>showPassportAuth(true);$('clear-data').parentElement.append(bindingButton);
const originalRenderAccountMode=renderAccountMode;
renderAccountMode=function(){originalRenderAccountMode();bindingButton.classList.toggle('hidden',accountMode!=='legacy'||!loggedIn);};
api('/api/me').then(signedIn).catch(()=>{if(!$('auth-dialog').open)showAuth();});

const localTrial=el('button','本地试用 / 注册','text-button');
localTrial.type='button';
localTrial.onclick=()=>{passportPreparation++;passportFlow='';passportLinkLegacy=false;authMode='register';$('password').value='';passportFormMode();$('auth-submit').disabled=false;};
$('auth-switch').parentElement.append(localTrial);

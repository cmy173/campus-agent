/* WebVPN credential management and sync status for the settings page. */
function renderWebvpnStatus(s){
  const p=$('webvpn-status');
  const syncNow=$('webvpn-sync-now'), clear=$('webvpn-clear');
  if(!s||!s.has_credentials){
    p.textContent='未保存 WebVPN 凭据：保存后服务会在后台自动登录，并按间隔同步学院通知。';
    syncNow.disabled=true;clear.disabled=true;
    return;
  }
  syncNow.disabled=false;clear.disabled=false;
  const sync=s.last_sync_at?('上次同步 '+new Date(s.last_sync_at).toLocaleString('zh-CN')+' · '+(s.last_sync_status||'未知')):'尚未同步';
  p.textContent='已保存账号 '+s.webvpn_username+'（通行证 '+s.zjuam_username+'）· '
    +(s.auto_sync?('自动同步每 '+s.sync_interval_minutes+' 分钟'):'自动同步已关闭')+' · '+sync;
}
async function loadWebvpnStatus(){
  if(typeof loggedIn==='undefined'||!loggedIn)return;
  try{renderWebvpnStatus(await api('/api/campus/webvpn/status'));}catch(e){/* settings page shows errors via toast on action */}
}
if($('webvpn-form')){
  $('webvpn-form').onsubmit=async e=>{
    e.preventDefault();
    const body={webvpn_username:$('webvpn-username').value.trim(),
      webvpn_password:$('webvpn-password').value,
      auto_sync:$('webvpn-auto-sync').checked};
    if($('webvpn-zjuam-username').value.trim())body.zjuam_username=$('webvpn-zjuam-username').value.trim();
    if($('webvpn-zjuam-password').value)body.zjuam_password=$('webvpn-zjuam-password').value;
    try{
      const r=await api('/api/campus/webvpn/credentials',{method:'POST',json:body});
      $('webvpn-password').value='';$('webvpn-zjuam-password').value='';
      toast(r.message||'凭据已保存');
      await loadWebvpnStatus();
    }catch(err){toast(err.message);}
  };
  $('webvpn-sync-now').onclick=async()=>{
    try{
      const r=await api('/api/campus/webvpn/sync',{method:'POST'});
      toast(r.message||'同步已开始');
      setTimeout(loadWebvpnStatus,6000);
    }catch(err){toast(err.message);}
  };
  $('webvpn-clear').onclick=async()=>{
    if(!confirm('清除已保存的 WebVPN 与通行证凭据、会话和同步状态？本地资料不受影响。'))return;
    try{
      await api('/api/campus/webvpn/credentials',{method:'DELETE'});
      toast('凭据已清除');
      await loadWebvpnStatus();
    }catch(err){toast(err.message);}
  };
}

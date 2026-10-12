// Exercise login rejection, cancellation, timeout and stale async responses.
const fs=require('node:fs');
const vm=require('node:vm');
const assert=require('node:assert/strict');
const test=require('node:test');

function deferred(){let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no});return {promise,resolve,reject};}
function element(text=''){
  const classes=new Set();
  return {textContent:text,value:'',disabled:false,open:false,children:[],
    classList:{add(...xs){xs.forEach(x=>classes.add(x))},toggle(x,enabled){if(enabled)classes.add(x);else classes.delete(x)},contains(x){return classes.has(x)}},
    append(...children){this.children.push(...children)},prepend(...children){this.children.unshift(...children)},
    removeAttribute(){},replaceChildren(){this.children=[]},close(){this.open=false},showModal(){this.open=true},
    parentElement:{append(){}},type:'button'};
}
function harness(login,prepare){
  const nodes=new Map(),calls=[],signed=[],timers=new Map();let prepared=0,nextTimer=0;
  const get=id=>{if(!nodes.has(id))nodes.set(id,element());return nodes.get(id)};
  get('auth-dialog').open=true;
  const ctx=vm.createContext({$:get,el:(_tag,text)=>element(text),authMode:'passport',loggedIn:false,
    accountMode:'offline',sessionEpoch:0,AbortController,
    setTimeout(fn,ms){const id=++nextTimer;timers.set(id,{fn,ms});return id},clearTimeout(id){timers.delete(id)},
    document:{querySelector:()=>get('main')},
    api:(path,options={})=>{
      if(path==='/api/me')return new Promise(()=>{});
      calls.push({path,json:structuredClone(options.json),signal:options.signal});
      if(path.endsWith('/cancel'))return ctx.cancel?ctx.cancel(options):Promise.resolve({ok:true});
      if(path.endsWith('/prepare'))return prepare?prepare(options,++prepared):Promise.resolve({flow_id:'flow-'+(++prepared),captcha:'data:image/png;base64,c3ludGhldGlj'});
      if(path.endsWith('/login'))return login(options);
      throw Error('Unexpected API in synthetic test');
    },signedIn:async me=>signed.push(me),showAuth(){},campusAction(){},
  });
  vm.runInContext(fs.readFileSync('campus_agent/static/passport.js','utf8'),ctx);
  return {get,ctx,calls,signed,timers,run:code=>vm.runInContext(code,ctx)};
}
async function flush(){await new Promise(resolve=>setImmediate(resolve))}
async function submit(h){h.get('username').value='synthetic-student';h.get('password').value='synthetic-password';h.get('authcode').value='1234';return h.get('auth-form').onsubmit({preventDefault(){}})}

test('authentication failure refreshes the flow, unlocks controls and never replays password',async()=>{
  const h=harness(async()=>{const error=Error('未能确认通行证认证');error.detail={code:'CAS_REJECTED',message:error.message};throw error});
  await h.run('preparePassport()');
  await submit(h);
  assert.equal(h.calls.filter(c=>c.path.endsWith('/login')).length,1);
  assert.equal(h.calls.filter(c=>c.path.endsWith('/prepare')).length,2);
  assert.equal(h.get('password').value,'');assert.equal(h.get('authcode').value,'');
  assert.equal(h.get('auth-submit').disabled,false);
  assert.equal(h.get('auth-refresh').disabled,false);assert.equal(h.get('auth-switch').disabled,false);
  assert.match(h.get('auth-error').textContent,/未能确认.*新的登录流程/);
  assert.equal(h.signed.length,0);
  h.get('auth-cancel').onclick();assert.equal(h.get('auth-dialog').open,false);
});

test('closing during login aborts waiting and ignores late success even after reopening',async()=>{
  const response=deferred();const h=harness(()=>response.promise);
  await h.run('preparePassport()');
  const pending=submit(h);
  const request=h.calls.find(c=>c.path.endsWith('/login'));
  assert.equal(h.get('auth-submit').disabled,true);
  h.get('auth-cancel').onclick();
  assert.equal(request.signal.aborted,true);assert.equal(h.get('auth-dialog').open,false);
  assert.equal(h.get('password').value,'');assert.equal(h.get('auth-switch').disabled,false);
  h.run('showPassportAuth()');await flush();
  const newFlow=h.run('passportFlow');assert.ok(newFlow);
  response.resolve({username:'synthetic-student',csrf:'synthetic',mode:'passport'});
  await pending;
  assert.equal(h.signed.length,0);assert.equal(h.run('passportFlow'),newFlow);
  assert.equal(h.get('auth-submit').disabled,false);
  assert.equal(h.calls.filter(c=>c.path.endsWith('/login')).length,1);
  assert.equal(h.calls.filter(c=>c.path.endsWith('/cancel')).length,1);
});

test('closing during preparation keeps dialog closed and rejects late captcha/flow',async()=>{
  const response=deferred();const h=harness(()=>Promise.resolve({}),()=>response.promise);
  const preparing=h.run('preparePassport()');await flush();
  const request=h.calls.find(c=>c.path.endsWith('/prepare'));
  h.get('auth-cancel').onclick();assert.equal(request.signal.aborted,true);
  response.resolve({flow_id:'late-flow',captcha:'late-image'});await preparing;
  assert.equal(h.get('auth-dialog').open,false);assert.equal(h.run('passportFlow'),'');
  assert.notEqual(h.get('auth-captcha-image').src,'late-image');
});

test('timeout leaves a recoverable fresh flow and sends no additional password',async()=>{
  const h=harness(options=>new Promise((_resolve,reject)=>options.signal.addEventListener('abort',()=>{const error=Error('aborted');error.name='AbortError';reject(error)})));
  await h.run('preparePassport()');const pending=submit(h);
  const timer=Array.from(h.timers.values()).find(t=>t.ms===180000);assert.ok(timer);timer.fn();
  await pending;
  assert.match(h.get('auth-error').textContent,/超时.*新的登录流程/);
  assert.equal(h.get('auth-submit').disabled,false);
  assert.equal(h.calls.filter(c=>c.path.endsWith('/login')).length,1);
  assert.equal(h.calls.filter(c=>c.path.endsWith('/cancel')).length,1);
});

test('new preparation waits for cancellation response so old cookie deletion cannot race it',async()=>{
  const cancelled=deferred();const h=harness(async()=>{throw Error('认证未确认')});
  await h.run('preparePassport()');h.ctx.cancel=()=>cancelled.promise;
  const pending=submit(h);await flush();
  assert.equal(h.calls.filter(c=>c.path.endsWith('/prepare')).length,1);
  cancelled.resolve({ok:true});await pending;
  assert.equal(h.calls.filter(c=>c.path.endsWith('/prepare')).length,2);
  assert.equal(h.get('auth-submit').disabled,false);
});

test('returning to current cache closes login without logging out that local account',async()=>{
  const h=harness(()=>Promise.resolve({}));h.ctx.loggedIn=true;
  await h.run('preparePassport()');h.get('auth-cache-back').onclick();await flush();
  assert.equal(h.get('auth-dialog').open,false);assert.equal(h.ctx.loggedIn,true);
  assert.equal(h.calls.filter(c=>c.path==='/api/auth/logout').length,0);
});

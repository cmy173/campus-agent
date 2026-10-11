// Exercise auth mode transitions without a real campus account or network.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
function element(text='') {
  return {textContent:text, value:'', classList:{toggle(){},add(){}},
    append(...children){this.children.push(...children)}, children:[],
    removeAttribute(){}, replaceChildren(){}, close(){}, parentElement:{append(){}}};
}
const nodes = new Map();
const get = id => {if(!nodes.has(id))nodes.set(id,element());return nodes.get(id)};
let trial, sent, signed;
get('auth-switch').parentElement.append = node => {trial=node};
const ctx = vm.createContext({
  $:get, el:(_tag,text)=>element(text), authMode:'passport', loggedIn:false,
  accountMode:'offline', sessionEpoch:0,
  api:(path,opts)=> {
    if(path==='/api/me')return new Promise(()=>{});
    sent={path,body:structuredClone(opts.json)};
    return Promise.resolve({username:'trial',csrf:'synthetic',mode:'legacy'});
  }, signedIn:async me=>{signed=me}, showAuth(){}, campusAction(){},
});
vm.runInContext(fs.readFileSync('campus_agent/static/passport.js','utf8'),ctx);
(async()=>{
  trial.onclick();
  assert.equal(ctx.authMode,'register');
  assert.equal(get('auth-submit').disabled,false);
  assert.equal(get('password').minLength,10);
  get('username').value='trial';get('password').value='independent-password';
  await get('auth-form').onsubmit({preventDefault(){}});
  assert.equal(sent.path,'/api/auth/register');
  assert.equal(sent.body.password,'independent-password');
  assert.equal(get('password').value,'');
  assert.equal(signed.mode,'legacy');
  ctx.authMode='passport';get('auth-switch').onclick();
  assert.equal(ctx.authMode,'login');
  get('password').value='independent-password';
  await get('auth-form').onsubmit({preventDefault(){}});
  assert.equal(sent.path,'/api/auth/login');
  console.log('PASS: local registration and login submit to separate local endpoints');
})().catch(error=>{console.error(error);process.exitCode=1});

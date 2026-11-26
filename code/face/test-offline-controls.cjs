const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const tick=()=>new Promise(setImmediate);
function setup(storage=new Map()){
  const elements=new Map(),handlers={};let stopped=0,offline=false;
  const server={camera_enabled:true,camera_mode:'auto',camera_active:true,watch_active:true,off_mode:false,muted:false,off_display:'eyes'};
  function element(){const classes=new Set();return {listeners:{},dataset:{},style:{setProperty(){}},elements:{},classList:{add:x=>classes.add(x),remove:x=>classes.delete(x),contains:x=>classes.has(x),toggle:(x,on)=>on?classes.add(x):classes.delete(x)},addEventListener(n,f){this.listeners[n]=f;},setAttribute(){},contains:()=>false,getContext:()=>({}),play:async()=>{}};}
  const document={head:{append(){}},visibilityState:'visible',body:element(),querySelector(q){if(!elements.has(q))elements.set(q,element());return elements.get(q);},createElement:element,addEventListener(){}};
  const context={document,localStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v)},screen:{width:1208,height:1392},navigator:{mediaDevices:{getUserMedia:async()=>({getTracks:()=>[{stop:()=>stopped++}]})}},Worker:class{postMessage(m){if(m.type==='init')Promise.resolve().then(()=>this.onmessage({data:{type:'ready'}}));}terminate(){}},MutationObserver:class{observe(){}},CustomEvent:class{constructor(type,{detail}){this.type=type;this.detail=detail;}},AbortController,URLSearchParams,location:{search:''},performance,console:{warn(){},error:console.error},setTimeout:()=>1,clearTimeout(){},setInterval(){},requestAnimationFrame(){},addEventListener(){},fetch:async(url,options={})=>{
    if(url.endsWith('/api/settings')){if(offline)throw Error('offline');const patch=JSON.parse(options.body);Object.assign(server,patch);if('camera_mode' in patch)server.camera_enabled=patch.camera_mode!=='off';server.camera_active=server.camera_enabled&&!server.off_mode&&(server.watch_active||server.camera_mode==='now');server.muted=server.off_mode||server.muted;}
    return {ok:true,json:async()=>url.endsWith('/api/temperature')?{temperature_c:30}:{...server}};
  }};
  context.window=context;context.addEventListener=(n,f)=>(handlers[n]??=[]).push(f);context.dispatchEvent=e=>(handlers[e.type]||[]).forEach(f=>f(e));
  vm.createContext(context);for(const file of ['face.js','vision.js'])vm.runInContext(fs.readFileSync(__dirname+'/'+file,'utf8'),context);
  return {context,server,storage,offline:v=>offline=v,stopped:()=>stopped,run:s=>vm.runInContext(s,context),element:q=>elements.get(q),click:q=>{const e=elements.get(q);if(q==='#vision-toggle'){e.value=e.value==='off'?'auto':e.value==='auto'&&!server.watch_active?'now':'off';e.listeners.change({stopPropagation(){}});}else e.listeners.click({stopPropagation(){}});}};
}
(async()=>{
  for(const control of ['#vision-toggle','#off-toggle']){
    const app=setup();await tick();assert.equal(app.context.faceVision.active,true);
    app.offline(true);app.click(control);await tick();
    assert.equal(app.context.faceVision.active,false,control+' must close the live camera without a server response');assert.equal(app.stopped(),1);
    await app.run('pollFace()');await tick();assert.equal(app.context.faceVision.active,false,'old server state cannot reopen camera');
    const reload=setup(app.storage);reload.offline(true);await tick();assert.equal(reload.context.faceVision.active,false,'pending close survives reload');
    app.offline(false);await app.run('pollFace()');await tick();assert.equal(app.server.camera_active,false,'close retries when agent recovers');
  }
  const app=setup();await tick();
  app.server.watch_active=false;app.server.camera_active=false;
  await app.run('pollFace()');await tick();
  assert.equal(app.element('#vision-toggle').value,'auto');
  app.click('#vision-toggle');await tick();
  assert.equal(app.context.faceVision.active,true,'now must open outside schedule after confirmation');
  assert.equal(app.element('#vision-toggle').value,'now');
  app.offline(true);app.click('#vision-toggle');await tick();
  assert.equal(app.context.faceVision.active,false,'manual off must win offline over now');
  assert.equal(app.element('#vision-toggle').value,'off');
  console.log('offline camera and full-off: immediate stop, stale poll, refresh persistence, retry passed');
})().catch(e=>{console.error(e);process.exitCode=1});

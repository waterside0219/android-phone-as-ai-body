const {chromium}=require(process.env.PLAYWRIGHT_PATH||'playwright');
const fs=require('node:fs'),http=require('node:http'),path=require('node:path'),assert=require('node:assert/strict');
const faceRoot=process.env.FACE_ROOT||__dirname,step=Number(process.env.STEP||1),evidence=process.env.EVIDENCE_DIR;
assert.ok(fs.existsSync(path.join(faceRoot,'index.html')),'test target must exist');
const server=http.createServer((req,res)=>{const relative=req.url.split('?')[0]==='/'?'index.html':req.url.split('?')[0];const override=path.join(faceRoot,relative);const file=fs.existsSync(override)?override:path.join(__dirname,relative);fs.readFile(file,(err,data)=>{if(err)return res.writeHead(404).end();res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript':file.endsWith('.html')?'text/html':'application/json');res.end(data);});});
(async()=>{await new Promise(r=>server.listen(0,'127.0.0.1',r));const browser=await chromium.launch({headless:true});try{
const page=await browser.newPage({viewport:{width:312,height:536}});const errors=[];page.on('pageerror',e=>errors.push(e.message));
let state={state:'在等',camera_mode:'off',camera_enabled:false,camera_active:false,presence:false,muted:true,off_mode:false,subtitle:'',subtitle_until:0,unread:0};
await page.route('http://127.0.0.1:8192/**',async route=>{const req=route.request();if(req.url().endsWith('/api/settings'))Object.assign(state,req.postDataJSON());await route.fulfill({json:req.url().includes('/api/messages')?{messages:[],unread:0,read_through:0}:state,headers:{'Access-Control-Allow-Origin':'*'}});});
await page.route('http://127.0.0.1:8191/**',r=>r.fulfill({body:'',contentType:'text/javascript'}));
await page.clock.install();await page.goto('http://127.0.0.1:'+server.address().port);await page.clock.pauseAt(new Date(Date.now()+1000));
for(const muted of [true,false]){
Object.assign(state,{subtitle:'五秒测试',subtitle_until:await page.evaluate(()=>Date.now()/1000+5),muted});await page.evaluate(data=>faceMessages.update(data),state);
assert.equal(await page.locator('#subtitle').evaluate(e=>e.classList.contains('visible')),true);
await page.clock.runFor(4990);assert.equal(await page.locator('#subtitle').evaluate(e=>e.classList.contains('visible')),true);
await page.clock.runFor(700);assert.equal(await page.locator('#subtitle').evaluate(e=>e.classList.contains('visible')),false,'both muted and audible captions expire');
}
await page.evaluate(()=>faceMessages.showCallSubtitle('电话字幕'));await page.clock.runFor(5700);assert.equal(await page.locator('#subtitle').evaluate(e=>e.classList.contains('visible')),false,'call subtitle expires');
state.system_notice='手机偏热';await page.evaluate(data=>{faceMessages.clearCallSubtitle();faceMessages.update(data);},state);await page.clock.runFor(5700);state.muted=false;await page.evaluate(data=>faceMessages.update(data),state);assert.equal(await page.locator('#subtitle').evaluate(e=>e.classList.contains('visible')),false,'notice and mute changes must not resurrect captions');
if(step>=2){
state.system_notice='';state.subtitle='';await page.evaluate(data=>faceMessages.update(data),state);
assert.equal(await page.locator('#settings-panel').count(),1,'settings panel exists');
await page.clock.runFor(6000);
assert.equal(await page.locator('#settings-open').evaluate(e=>getComputedStyle(e).opacity),'0');
assert.equal(await page.locator('#control-bar').isVisible(),false);
await page.evaluate(()=>showControls());
assert.equal(await page.locator('#control-bar').isVisible(),false,'touch exposes one entry only');
await page.click('#settings-open');assert.equal(await page.locator('#settings-panel').isVisible(),true);
for(const id of ['vision-toggle','mute-toggle','off-toggle','watch-form','silent-form','switch','close-hours','call-link','messages-open'])assert.equal(await page.locator('#'+id).evaluate(e=>!!e.closest('#settings-panel')),true,id+' is in settings');
await page.selectOption('#vision-toggle','off');await Promise.all([page.waitForResponse(r=>r.url().endsWith('/api/settings')&&r.request().postDataJSON().camera_mode==='now'),page.selectOption('#vision-toggle','now')]);assert.equal(state.camera_mode,'now');
await page.fill('#silent-start','08:30');await page.fill('#silent-end','17:30');await Promise.all([page.waitForResponse(r=>r.url().endsWith('/api/settings')&&r.request().postDataJSON().work_silent_period),page.click('#silent-save')]);assert.equal(state.work_silent_period.start,'08:30');
await page.click('#messages-open');assert.equal(await page.locator('#message-panel').isVisible(),true);assert.equal(await page.locator('#settings-panel').isHidden(),true);await page.click('#message-close');
for(const viewport of [{width:412,height:980,name:'inner'},{width:312,height:536,name:'outer'}]){
await page.setViewportSize(viewport);await page.clock.runFor(6000);await new Promise(r=>setTimeout(r,650));
if(evidence)await page.screenshot({path:path.join(evidence,'2-'+viewport.name+'-idle-simulated.png')});
state.unread=1;await page.evaluate(data=>faceMessages.update(data),state);await new Promise(r=>setTimeout(r,650));
assert.equal(await page.locator('#message-peek').evaluate(e=>getComputedStyle(e).backgroundColor),'rgba(0, 0, 0, 0)');
assert.equal(await page.locator('#message-unread').evaluate(e=>e.getBoundingClientRect().width),8);
assert.ok(await page.locator('#message-unread').evaluate(e=>e.getBoundingClientRect().top>=48),'unread dot clears the outer-screen native camera indicator');
if(evidence)await page.screenshot({path:path.join(evidence,'2-'+viewport.name+'-unread-simulated.png')});
await page.evaluate(()=>showControls());await page.click('#settings-open');if(evidence)await page.screenshot({path:path.join(evidence,'2-'+viewport.name+'-settings-simulated.png')});await page.click('#settings-close');state.unread=0;
}
}
if(step>=3){
state.presence=false;await page.evaluate(()=>{presence=false;window.faceCall=null;speakingUntil=0;lastActiveAt=Date.now()-301000;setMode('live2d',false);updateIdleDisplay();});
assert.equal(await page.evaluate(()=>mode),'eyes','existing five-minute Live2D fallback retained');
await page.evaluate(()=>{lastPresenceAt=Date.now()-599000;updateIdleDisplay();});assert.equal(await page.evaluate(()=>pageDisplay().rest_stage),'active');
await page.evaluate(()=>{lastPresenceAt=Date.now()-601000;updateIdleDisplay();});assert.equal(await page.evaluate(()=>pageDisplay().rest_stage),'dim');
await page.evaluate(()=>{lastPresenceAt=Date.now()-1801000;updateIdleDisplay();});assert.equal(await page.evaluate(()=>pageDisplay().rest_stage),'doze');
if(evidence)await page.screenshot({path:path.join(evidence,'3-doze-simulated.png')});
await page.evaluate(()=>showControls());assert.equal(await page.evaluate(()=>pageDisplay().rest_stage),'active','touch immediately restores');
await page.evaluate(()=>{lastPresenceAt=Date.now()-1801000;presence=true;updateIdleDisplay();});assert.equal(await page.evaluate(()=>pageDisplay().rest_stage),'active','presence restores');
const offsets=await page.evaluate(()=>{presence=false;const a=pageDisplay().drift;nextDriftAt=0;updateIdleDisplay();return [a,pageDisplay().drift];});assert.notDeepEqual(offsets[0],offsets[1]);for(const n of Object.values(offsets[1]))assert.ok(Math.abs(n)<=12);
console.log('absence thresholds, touch/presence recovery, drift and existing idle fallback passed');
}
if(step>=4){
assert.equal(await page.evaluate(()=>typeof window.eyeExpressions?.update),'function','replaceable expression renderer');
const result=await page.evaluate(()=>{
  const render=(state,present=false,reaction=null,now=0)=>{eyeExpressions.update({state,present,reaction,now});return eyeExpressions.diagnostic().name;};
  const states=['thinking','working','waiting','stuck'].map(state=>({state,first:render(state),later:render(state,false,null,21000)}));
  const waiting=render('waiting',false,null,30000),present=render('waiting',true,null,30001);
  const touch=render('waiting',true,'hold',30002),release=render('waiting',true,null,30003);
  return {states,waiting,present,touch,release};
});
for(const state of result.states){if(state.state==='waiting'){assert.equal(state.first,'normal');assert.equal(state.later,'normal','idle remains normal');}else assert.notEqual(state.first,state.later,state.state+' varies over time');}
assert.equal(result.waiting,'normal');assert.equal(result.present,'normal','presence alone does not cycle idle expressions');assert.equal(result.touch,'heart');assert.notEqual(result.release,'heart');
await page.evaluate(()=>{eyeExpressions.presets.test={...eyeExpressions.presets.half,frame:[0,2,-1]};eyeExpressions.sequences.waiting=['test'];eyeExpressions.update({state:'waiting',present:false,reaction:null,now:60000});eyeExpressions.update({state:'waiting',present:false,reaction:null,now:60800});});
assert.equal(await page.locator('#simple').getAttribute('data-expression'),'test','replacing parameter data changes rendered expression');
assert.match(await page.locator('#glasses-rig').getAttribute('transform'),/rotate\(-1.000/);
console.log('four states, work variants, stable idle/presence, touch and replaceable shape parameters passed');
}
await new Promise(r=>setTimeout(r,700));assert.equal(await page.locator('#subtitle').evaluate(e=>getComputedStyle(e).opacity),'0','expired subtitle is visually hidden, not just untagged');
assert.deepEqual(errors,[]);console.log('v3 step '+step+' passed');
}finally{await browser.close();server.close();}})().catch(e=>{console.error(e);server.close();process.exitCode=1;});

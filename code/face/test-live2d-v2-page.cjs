const {chromium}=require(process.env.PLAYWRIGHT_PATH||'playwright');
const fs=require('node:fs'),http=require('node:http'),path=require('node:path'),assert=require('node:assert/strict');
const evidence=process.env.EVIDENCE_DIR||path.join(__dirname,'../.private/backup-20261029-H');
const server=http.createServer((req,res)=>{const p=new URL(req.url,'http://local').pathname,file=path.join(__dirname,p==='/'?'index.html':p);fs.readFile(file,(err,data)=>{if(err)return res.writeHead(404).end();res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript':file.endsWith('.html')?'text/html':file.endsWith('.png')?'image/png':'application/json');res.end(data);});});
(async()=>{await new Promise(r=>server.listen(0,'127.0.0.1',r));const browser=await chromium.launch({headless:true});try{
 const page=await browser.newPage({viewport:{width:312,height:536}}),errors=[],touches=[],responses=[];
 let state={state:'在等',camera_mode:'off',camera_enabled:false,camera_active:false,presence:false,muted:true,off_mode:false,thermal_hold:false,subtitle:'',subtitle_until:0,unread:0};
 page.on('pageerror',e=>errors.push(e.message));page.on('response',r=>{if(r.url().includes('/runtime/')||r.url().includes('cubism-framework'))responses.push({url:r.url(),status:r.status()});});
 await page.route('http://127.0.0.1:8192/**',async route=>{const req=route.request(),p=new URL(req.url()).pathname;if(p==='/api/touch')touches.push(req.postDataJSON().kind);await route.fulfill({json:p==='/api/messages'?{messages:[],unread:0,read_through:0}:state,headers:{'Access-Control-Allow-Origin':'*'}});});
 await page.route('http://127.0.0.1:8191/**',r=>r.fulfill({body:'',contentType:'text/javascript'}));
 await page.goto('http://127.0.0.1:'+server.address().port+'/?mode=live2d');
 await page.waitForTimeout(1500);
 assert.equal(await page.evaluate(()=>!!window.faceLive2D?.model),true,'phone face entry must load the independent V2 renderer');
 assert.equal(await page.evaluate(()=>faceLive2D.model.parameters.count),24);
 // Use the existing page clock/RAF, not an independent animation owner.
 await page.clock.install();await page.clock.pauseAt(new Date(Date.now()+100));
 for(const side of ['L','R']){
  await page.evaluate(()=>{faceLive2D.reset();presence=false;touchLookUntil=0;});
  const point=await page.evaluate(s=>faceLive2D.earPoint(s),side);
  const before=touches.length;await page.mouse.click(point.x,point.y);await page.clock.runFor(500);
  const pose=await page.evaluate(()=>Object.fromEntries(faceLive2D.model.parameters.ids.map((k,i)=>[k,faceLive2D.model.parameters.values[i]])));
  assert.ok(Math.abs(pose['ParamEar'+side])>.01,side+' hit is routed to one ear');
  assert.ok(Math.abs(pose['ParamEar'+(side==='L'?'R':'L')])<.0001,'other ear remains still');
  for(const k of Object.keys(pose).filter(k=>k.startsWith('ParamHair')))assert.ok(Math.abs(pose[k])<.0001,'ear click must not trigger look/head/hair: '+k);
  assert.equal(touches.length,before,'ear is not also sent as a generic tap');
  await page.clock.runFor(2600);
 }
 await page.mouse.click(150,350);await page.clock.runFor(450);assert.equal(touches.at(-1),'tap','non-ear tap still reports');
 await page.mouse.move(150,350);await page.mouse.down();await page.clock.runFor(400);await page.mouse.up();assert.equal(touches.at(-1),'hold');
 await page.mouse.move(140,350);await page.mouse.down();await page.mouse.move(200,350);await page.clock.runFor(50);await page.mouse.up();assert.equal(touches.at(-1),'chin');
 const captures=[];
 for(const viewport of [{width:312,height:536,name:'outer'},{width:412,height:980,name:'inner'},{width:311,height:535,name:'outer-L'}]){
  await page.setViewportSize(viewport);await page.evaluate(()=>{setReaction(null);touchLookUntil=0;faceLive2D.reset();});await page.clock.runFor(300);
  await page.evaluate(()=>faceMessages.showCallSubtitle('这是一条本机模拟字幕。'));await page.clock.runFor(500);
  const geo=await page.evaluate(()=>({excluded:faceExclusion(),subtitle:(()=>{const r=document.querySelector('#subtitle').getBoundingClientRect();return {left:r.left,top:r.top,right:r.right,bottom:r.bottom};})(),ears:['L','R'].map(s=>({side:s,...faceLive2D.earPoint(s)})),canvas:{width:canvas.width,height:canvas.height,cssWidth:canvas.clientWidth,cssHeight:canvas.clientHeight}}));
  assert.ok(geo.subtitle.top>=geo.excluded.bottom,'subtitle clears current V2 face at '+viewport.name);
  assert.ok(geo.excluded.top>=-10,'ear tips stay in viewport at '+viewport.name);
  for(const ear of geo.ears)assert.ok(ear.x>0&&ear.x<viewport.width&&ear.y>0&&ear.y<viewport.height,'ear target remains reachable');
  assert.ok(Math.abs(geo.canvas.width/geo.canvas.height-geo.canvas.cssWidth/geo.canvas.cssHeight)<.002,'uniform scaling preserves 960:1280 artwork');
  captures.push({viewport,...geo});await page.screenshot({path:path.join(evidence,'page-'+viewport.name+'-local.png')});
 }
 await page.evaluate(()=>{lastActiveAt=Date.now()-300001;presence=false;window.faceCall=null;speakingUntil=0;updateIdleDisplay();});assert.equal(await page.evaluate(()=>mode),'eyes');
 await page.evaluate(()=>showControls());assert.equal(await page.evaluate(()=>mode),'live2d');
 for(let i=0;i<3;i++){await page.evaluate(()=>setMode('eyes'));await page.evaluate(()=>setMode('live2d'));await page.clock.runFor(30);}
 const count=responses.filter(r=>r.url.includes('.moc3')).length;assert.equal(count,1,'switches reuse the model, not duplicate it');
 state.thermal_hold=true;await page.evaluate(()=>{faceLive2D.pokeEar('L');setCooling(true);});const coolingFrame=await page.evaluate(()=>lastFrame);await page.clock.runFor(5000);
 assert.equal(await page.evaluate(()=>lastFrame),coolingFrame,'cooling stops the existing RAF');assert.equal(await page.locator('#live').isVisible(),false);
 assert.equal(await page.locator('#simple').evaluate(e=>getComputedStyle(e).visibility),'visible');state.thermal_hold=false;await page.evaluate(()=>setCooling(false));await page.clock.runFor(50);
 assert.ok(await page.evaluate(()=>lastFrame)>coolingFrame);assert.equal(await page.evaluate(()=>faceLive2D.model.parameters.values[faceLive2D.model.parameters.ids.indexOf('ParamEarL')]),0);
 // Visibility is simulated here; lifecycle listener must reset dynamics immediately.
 await page.evaluate(()=>{Object.defineProperty(document,'visibilityState',{configurable:true,get:()=> 'hidden'});document.dispatchEvent(new Event('visibilitychange'));});
 const hiddenBefore=await page.evaluate(()=>Array.from(faceLive2D.model.parameters.values));await page.clock.runFor(3000);assert.deepEqual(await page.evaluate(()=>Array.from(faceLive2D.model.parameters.values)),hiddenBefore);
 await page.evaluate(()=>{delete document.visibilityState;document.dispatchEvent(new Event('visibilitychange'));});await page.clock.runFor(50);
 assert.equal(await page.evaluate(()=>faceLive2D.model.parameters.values[faceLive2D.model.parameters.ids.indexOf('ParamMouthOpenY')]),0);
 await page.evaluate(()=>dispatchEvent(new PageTransitionEvent('pagehide',{persisted:true})));
 assert.equal(await page.evaluate(()=>!!window.faceLive2D?.model),true,'bfcache preserves model for return');
 await page.evaluate(()=>dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true})));await page.clock.runFor(50);
 await page.reload();await page.waitForFunction(()=>!!window.faceLive2D?.model);assert.equal(await page.evaluate(()=>mode),'live2d','reload restores remembered mode');
 assert.ok(responses.every(r=>r.status===200));assert.deepEqual(errors,[]);
 fs.writeFileSync(path.join(evidence,'page-local.json'),JSON.stringify({captures,responses,errors,touches,kind:'Mac browser simulation, not phone acceptance'},null,2));
 console.log('V2 real face page: ear routing, old gestures, subtitle/layout, five-minute fallback, cooling, visibility, switching and reload passed');
}finally{await browser.close();server.close();}})().catch(e=>{console.error(e);server.close();process.exitCode=1;});

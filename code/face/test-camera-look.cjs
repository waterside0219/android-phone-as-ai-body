// K: real vision.js, synthetic video, no detected person and a cold Worker.
const {chromium}=require(process.env.PLAYWRIGHT_PATH||'playwright');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
(async()=>{const browser=await chromium.launch({headless:true});try{
  const page=await browser.newPage({viewport:{width:312,height:536}}),frames=[],errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/*',r=>r.abort());
  await page.setContent(fs.readFileSync(path.join(__dirname,'index.html'),'utf8').replace(/<script[\s\S]*?<\/script>/g,''));
  await page.clock.install({time:new Date('2026-10-29T15:40:00Z')});
  await page.exposeFunction('uploadLook',frame=>{frames.push(frame);return {ok:true,selected:'look'};});
  await page.evaluate(()=>{
    window.saveControls=()=>{};window.setReaction=()=>{};
    window.postJSON=async(url,frame)=>url==='/api/camera-frame'?uploadLook(frame):{};
    const source=document.createElement('canvas');source.width=960;source.height=540;
    const c=source.getContext('2d');c.fillStyle='#6594a2';c.fillRect(0,0,960,540);
    const draw=CanvasRenderingContext2D.prototype.drawImage;CanvasRenderingContext2D.prototype.drawImage=function(s,...args){return draw.call(this,s instanceof HTMLVideoElement?source:s,...args);};
    for(const [key,value]of Object.entries({readyState:4,videoWidth:960,videoHeight:540}))Object.defineProperty(HTMLVideoElement.prototype,key,{get:()=>value});
    window.frozenVideo=false;Object.defineProperty(HTMLVideoElement.prototype,'currentTime',{get:()=>frozenVideo?10:performance.now()/1000});
    Object.defineProperty(HTMLVideoElement.prototype,'srcObject',{set(){}});HTMLVideoElement.prototype.play=async()=>{};
    window.starts=0;window.stops=0;
    Object.defineProperty(navigator,'mediaDevices',{value:{getUserMedia:async()=>{starts++;return {getTracks:()=>[{readyState:'live',stop(){stops++;this.readyState='ended';}}]};}}});
    window.readyWorker=false;
    window.Worker=class{constructor(){window.testWorker=this;}postMessage(m){if(m.type==='init'&&readyWorker)queueMicrotask(()=>this.onmessage({data:{type:'ready'}}));}terminate(){}};
    window.createImageBitmap=async()=>({close(){}});
  });
  await page.addScriptTag({path:path.join(__dirname,'vision.js')});
  const defaults={camera_mode:'auto',camera_enabled:true,camera_allowed:false,watch_active:false,off_mode:false,thermal_hold:false};
  for(const call of ['idle','connecting','connected','recovering']){
    await page.evaluate(call=>{window.faceCall={diagnostic:()=>({state:call})};dispatchEvent(new CustomEvent('face-call-active'));},call);
    const start=await page.evaluate(()=>Date.now()/1000),before=frames.length;
    const detail={...defaults,camera_allowed:true,camera_look_until:start+60};
    await page.evaluate(detail=>dispatchEvent(new CustomEvent('face-settings',{detail})),detail);
    await page.clock.runFor(500);
    await page.evaluate(detail=>dispatchEvent(new CustomEvent('face-settings',{detail})),detail);
    await page.waitForTimeout(100);
    assert.equal(frames.length,before+1,`${call}: look must capture even before the Worker is ready and without presence`);
    assert.equal(frames.at(-1).reason,'look');assert.ok(frames.at(-1).jpeg_base64.length>100);
    assert.ok(frames.at(-1).captured_at>=start);
    await page.evaluate(detail=>dispatchEvent(new CustomEvent('face-settings',{detail})),detail);
    await page.waitForTimeout(30);assert.equal(frames.length,before+1,'one frame per look request');
    await page.evaluate(detail=>dispatchEvent(new CustomEvent('face-settings',{detail})),defaults);
    assert.equal(await page.evaluate(()=>faceVision.streamAlive),false,'return to out-of-hours policy');
    await page.clock.runFor(10);
  }
  // Closed eyes are not a hard camera-off choice for this explicit request.
  await page.evaluate(()=>{readyWorker=true;dispatchEvent(new CustomEvent('face-closed-change',{detail:{closed:true}}));});
  const before=frames.length;
  await page.evaluate(detail=>dispatchEvent(new CustomEvent('face-settings',{detail:{...detail,camera_allowed:true,camera_look_until:Date.now()/1000+60}})),defaults);
  await page.clock.runFor(500);await page.waitForTimeout(50);
  assert.equal(frames.length,before+1);
  // A second look must wait for advancing video, never relabel a frozen image.
  const stalled=await page.evaluate(()=>{frozenVideo=true;return Date.now()/1000+60;});
  await page.evaluate(detail=>dispatchEvent(new CustomEvent('face-settings',{detail})),{...defaults,camera_allowed:true,camera_look_until:stalled});
  await page.clock.runFor(1000);await page.waitForTimeout(30);
  assert.equal(frames.length,before+1,'stalled video cannot masquerade as a fresh frame');
  await page.evaluate(()=>{frozenVideo=false;});
  await page.evaluate(detail=>dispatchEvent(new CustomEvent('face-settings',{detail})),{...defaults,camera_allowed:true,camera_look_until:stalled});
  await page.waitForTimeout(50);assert.equal(frames.length,before+2);
  await page.clock.runFor(58000);
  assert.equal(await page.evaluate(()=>faceVision.streamAlive),true,'camera is alive before the lease expires');
  await page.clock.runFor(1100);
  assert.equal(await page.evaluate(()=>faceVision.streamAlive),false,'expired lease must stop even without another settings poll');
  assert.deepEqual(errors,[]);console.log('PASS 23:40 idle/connecting/connected/recovering, cold model/no presence, one fresh frame, closed-eye override and 60s expiry');
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exitCode=1;});

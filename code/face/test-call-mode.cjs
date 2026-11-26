// Local browser with synthetic pixels/Worker/API. Never opens a real camera.
const {chromium}=require(process.env.PLAYWRIGHT_PATH||'playwright');
const fs=require('node:fs'),http=require('node:http'),path=require('node:path'),assert=require('node:assert/strict');
const faceRoot=process.env.FACE_ROOT||__dirname;
const server=http.createServer((req,res)=>{const relative=req.url.split('?')[0]==='/'?'index.html':req.url.split('?')[0];const candidate=path.join(faceRoot,relative);const file=fs.existsSync(candidate)?candidate:path.join(__dirname,relative);fs.readFile(file,(e,data)=>{if(e)return res.writeHead(404).end();res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript':file.endsWith('.html')?'text/html':'application/json');res.end(data);});});
(async()=>{await new Promise(r=>server.listen(0,'127.0.0.1',r));const browser=await chromium.launch({headless:true});try{
  const page=await browser.newPage({viewport:{width:312,height:536}}),frames=[],touches=[],errors=[],settings=[];
  let readySent=false;
  const state={state:'在等',camera_mode:'auto',camera_active:true,camera_enabled:true,watch_active:true,presence:true,muted:true,off_mode:false,closed_until:0,notify_sadness:true,companion_until:0};
  page.on('pageerror',e=>errors.push(e.message));
  await page.route('http://127.0.0.1:8192/**',async r=>{const p=new URL(r.request().url()).pathname,body=r.request().postDataJSON();
    if(p==='/api/camera-frame')frames.push(body);
    if(p==='/api/touch')touches.push(body.kind);
    if(p==='/api/settings'){settings.push(body);Object.assign(state,body);if('companion_hours'in body)state.companion_until=body.companion_hours?await page.evaluate(h=>Date.now()/1000+h*3600,body.companion_hours):0;delete state.companion_hours;}
    await r.fulfill({json:p==='/api/temperature'?{temperature_c:30,thermal_hold:false}:p==='/api/camera-frame'?{ok:true,selected:'changed'}:p==='/api/messages'?{messages:[],unread:0}:state,headers:{'Access-Control-Allow-Origin':'*'}});
  });
  await page.route('http://127.0.0.1:8191/**',async r=>{
    const p=new URL(r.request().url()).pathname;
    if(p==='/app.js')return r.fulfill({body:fs.readFileSync(path.join(__dirname,'../call-web/app.js'),'utf8'),contentType:'text/javascript'});
    if(p==='/api/incoming')return r.fulfill({json:{incoming:null}});
    if(p.endsWith('/events')&&r.request().method()==='GET'){
      if(!readySent){readySent=true;return r.fulfill({json:{events:[{data:{type:'ready'}}],cursor:1}});}
      await new Promise(resolve=>setTimeout(resolve,10));if(!r.request().failure())await r.fulfill({json:{events:[],cursor:1}}).catch(()=>{});return;
    }
    return r.fulfill({json:{ok:true,remote_confirmed:true}});
  });
  await page.addInitScript(()=>{
    const source=document.createElement('canvas');source.width=960;source.height=720;const c=source.getContext('2d');
    const gradient=c.createLinearGradient(0,0,960,0);gradient.addColorStop(0,'black');gradient.addColorStop(1,'white');c.fillStyle=gradient;c.fillRect(0,0,960,720);
    window.noisyFrame=()=>{const image=c.createImageData(960,720);let value=17;for(let i=0;i<image.data.length;i+=4){value=(value*1664525+1013904223)>>>0;image.data[i]=value&255;image.data[i+1]=(value>>>8)&255;image.data[i+2]=(value>>>16)&255;image.data[i+3]=255;}c.putImageData(image,0,0);};
    const draw=CanvasRenderingContext2D.prototype.drawImage;CanvasRenderingContext2D.prototype.drawImage=function(s,...args){return draw.call(this,s instanceof HTMLVideoElement?source:s,...args);};
    for(const [key,value]of Object.entries({readyState:4,videoWidth:960,videoHeight:720}))Object.defineProperty(HTMLVideoElement.prototype,key,{get:()=>value});
    Object.defineProperty(HTMLVideoElement.prototype,'currentTime',{get:()=>performance.now()/1000});
    Object.defineProperty(HTMLVideoElement.prototype,'srcObject',{set(){}});HTMLVideoElement.prototype.play=async()=>{};
    class AudioContextMock{
      constructor(){this.state='running';this.sampleRate=16000;this.audioWorklet={addModule:async()=>{}};this.destination={};}
      createGain(){return {gain:{value:0},connect(){},disconnect(){}};}
      createMediaStreamSource(){return {connect(){},disconnect(){}};}
      async resume(){this.state='running';} async close(){this.state='closed';}
    }
    window.AudioContext=AudioContextMock;window.AudioWorkletNode=class{constructor(){this.port={};}connect(){}disconnect(){}};
    window.stops=0;Object.defineProperty(navigator,'mediaDevices',{value:{getUserMedia:async constraints=>({getTracks:()=>[{readyState:'live',stop(){if(!constraints.audio)window.stops++;this.readyState='ended';}}],getAudioTracks:()=>[{}]}),enumerateDevices:async()=>[]}});
    window.createImageBitmap=async()=>({close(){}});
    window.Worker=class{constructor(){window.testWorker=this;}postMessage(m){if(m.type==='init')queueMicrotask(()=>this.onmessage({data:{type:'ready'}}));}terminate(){}emit(present=true){this.onmessage({data:{type:'result',face:{present,look:{x:0,y:0},looking:false}}});}};
  });
  await page.clock.install();await page.goto('http://127.0.0.1:'+server.address().port);await page.waitForFunction(()=>window.faceVision?.active);
  async function advance(ms){while(ms>0){const step=Math.min(ms,1000);await page.clock.fastForward(step);ms-=step;}}
  const failures=[];function check(fn){try{fn();}catch(e){failures.push(e.message);}}
  await page.evaluate(()=>testWorker.emit());await page.waitForTimeout(50);
  const before=frames.length;await advance(60000);await page.evaluate(()=>testWorker.emit());await page.waitForTimeout(50);
  const baselineFrames=frames.length-before;
  check(()=>assert.equal(baselineFrames,1,'baseline automatic capture interval is one minute'));
  const original=await page.locator('.glasses-rim').first().evaluate(e=>getComputedStyle(e).fill);
  await page.evaluate(()=>faceCall.dial());await page.waitForFunction(()=>faceCall.diagnostic().state==='connected');
  await page.waitForTimeout(50);
  const atConnect=await page.locator('#call-status').evaluate(e=>getComputedStyle(e).visibility);
  check(()=>assert.equal(atConnect,'hidden','connected status is hidden without a touch'));
  const tint=await page.locator('.glasses-rim').first().evaluate(e=>getComputedStyle(e).fill);
  check(()=>assert.equal(tint,'rgb(139, 215, 203)','connected frame is pale turquoise'));
  await page.evaluate(()=>eyeExpressions.selectOutfit('halloween'));
  const costumeTint=await page.locator('.glasses-rim').first().evaluate(e=>getComputedStyle(e).fill);
  check(()=>assert.equal(costumeTint, 'rgb(139, 215, 203)','costume keeps call tint'));
  // The actual app updates hidden/text on every settings tick; these must not reveal status.
  await page.evaluate(detail=>dispatchEvent(new CustomEvent('face-settings',{detail})),state);
  await advance(5000);
  const afterFive=await page.locator('#call-status').evaluate(e=>getComputedStyle(e).visibility);
  check(()=>assert.equal(afterFive,'hidden','default remains hidden after five seconds'));
  await page.mouse.click(150,180);await advance(100);
  const visible=await page.locator('#call-status').evaluate(e=>getComputedStyle(e).visibility);
  check(()=>assert.equal(visible,'visible','screen touch reveals status'));
  await advance(2800);
  const at2900=await page.locator('#call-status').evaluate(e=>getComputedStyle(e).visibility);
  check(()=>assert.equal(at2900,'visible','status remains visible before three seconds'));
  await advance(300);
  const hidden=await page.locator('#call-status').evaluate(e=>getComputedStyle(e).visibility);
  check(()=>assert.equal(hidden,'hidden','status hides at three seconds despite settings polling'));
  const callBefore=frames.length;
  await advance(52000);await page.evaluate(()=>testWorker.emit());await page.waitForTimeout(50);
  check(()=>assert.equal(frames.length,callBefore,'call does not take automatic JPEG at one minute'));
  await advance(60000);await page.evaluate(()=>testWorker.emit());await page.waitForTimeout(50);
  check(()=>assert.equal(frames.length,callBefore+1,'call still takes JPEG at two minutes'));
  const alive=await page.evaluate(()=>({active:faceVision.active,alive:faceVision.streamAlive,stops,interval:faceVision.detectionInterval}));
  check(()=>assert.deepEqual(alive,{active:true,alive:true,stops:0,interval:200},'vision remains on at its original cadence'));
  const callAutoFrames=frames.length-callBefore;
  const metrics=await page.evaluate(()=>pageDisplay().vision);
  check(()=>assert.equal(metrics?.snapshot_interval_ms,120000,'diagnostic reports actual call capture interval'));
  const render=await page.evaluate(()=>pageDisplay().render);
  check(()=>assert.equal(Number.isFinite(render?.fps)&&render.fps>0,true,'render diagnostic contains a measured frame rate'));
  check(()=>assert.equal(metrics?.detection_count>=4,true,'diagnostic counts actual Worker results'));
  // Explicit look-now remains immediate even during a call.
  await page.evaluate(()=>dispatchEvent(new CustomEvent('face-look-now')));await page.waitForTimeout(50);
  check(()=>assert.equal(frames.at(-1).reason,'manual'));
  if(process.env.EVIDENCE_DIR)await page.screenshot({path:path.join(process.env.EVIDENCE_DIR,'F2-connected-local.png')});
  await page.locator('#call-end').dispatchEvent('click');await page.waitForFunction(()=>faceCall.diagnostic().state==='idle');await page.waitForTimeout(50);
  const restored=await page.locator('.glasses-rim').first().evaluate(e=>getComputedStyle(e).fill);
  check(()=>assert.equal(restored,original,'hangup restores original frame'));
  const after=frames.length;await advance(61000);await page.evaluate(()=>testWorker.emit());await page.waitForTimeout(50);
  check(()=>assert.equal(frames.length,after+1,'hangup restores one-minute JPEG cadence'));
  check(()=>assert.deepEqual(errors,[]));
  console.log(JSON.stringify({kind:'local synthetic browser only',baseline_frames_per_60s:baselineFrames,call_auto_frames_in_120s:callAutoFrames,alive,failures},null,2));
  assert.deepEqual(failures,[]);
  console.log('F2 actual call-client lifecycle, default hidden / 3s touch, frame tint, vision/capture and hangup passed');
}finally{await browser.close();server.closeAllConnections();server.close();}})().catch(e=>{console.error(e);server.closeAllConnections();server.close();process.exitCode=1;});

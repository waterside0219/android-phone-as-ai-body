// Real Core/Framework/WebGL on the Mac; only phone APIs are simulated.
const {chromium}=require(process.env.PLAYWRIGHT_PATH||'playwright');
const fs=require('node:fs'),http=require('node:http'),path=require('node:path'),assert=require('node:assert/strict');
const evidence=process.env.EVIDENCE_DIR||fs.mkdtempSync(path.join(require('node:os').tmpdir(),'live2d-v2-'));
const server=http.createServer((req,res)=>{
 const name=new URL(req.url,'http://local').pathname;
 if(name==='/harness')return res.setHeader('Content-Type','text/html').end('<canvas id="live" style="width:312px;height:536px"></canvas><script src="/vendor/live2dcubismcore.min.js"></script>');
 const file=path.join(__dirname,name==='/'?'index.html':name);
 fs.readFile(file,(err,data)=>{if(err)return res.writeHead(404).end();res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript':file.endsWith('.html')?'text/html':file.endsWith('.png')?'image/png':'application/json');res.end(data);});
});
(async()=>{await new Promise(r=>server.listen(0,'127.0.0.1',r));const browser=await chromium.launch({headless:true});try{
 const page=await browser.newPage({viewport:{width:412,height:700}}),errors=[],requests=[];
 page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>requests.push(r.url()));
 await page.goto('http://127.0.0.1:'+server.address().port+'/harness');
 const loaded=await page.evaluate(async()=>{try{window.v2=await import('/live2d-v2.js');return typeof v2.loadV2==='function';}catch{return false;}});
 assert.equal(loaded,true,'V2 renderer must be loadable as a local module');
 await page.evaluate(async()=>{window.r=await v2.loadV2(document.querySelector('#live'));window.t=0;window.sample=()=>Object.fromEntries(r.model.parameters.ids.map((id,i)=>[id,r.model.parameters.values[i]]));window.step=(n=1,input={})=>{for(let i=0;i<n;i++)r.frame(t+=1000/60,input);return sample();};});
 const initial=await page.evaluate(()=>({params:r.model.parameters.count,core:Live2DCubismCore.Version.csmGetVersion(),data:step()}));
 assert.equal(initial.params,24);assert.equal(initial.core,83951616);assert.equal(initial.data.ParamMouthOpenY,0);
 const physics=await page.evaluate(()=>{r.reset();t=0;step(60,{look:{x:0,y:0}});const rows=[];for(let i=0;i<240;i++){const p=step(1,{look:{x:i<30?1:0,y:0}});rows.push(Object.fromEntries(Object.entries(p).filter(([k])=>k.startsWith('ParamHair'))));}return rows;});
 const hair=Object.keys(physics[0]);assert.equal(hair.length,6);for(const k of hair)assert.ok(Math.max(...physics.map(row=>Math.abs(row[k])))>.0001,k+' must respond to head input');
 assert.ok(new Set(hair.map(k=>JSON.stringify(physics.map(r=>r[k])))).size>=5,'hair groups have distinct inertia');
 for(const side of ['L','R']){
  const trace=await page.evaluate(side=>{r.reset();t=0;step(60,{look:{x:0,y:0}});r.pokeEar(side);return Array.from({length:240},()=>step(1,{look:{x:0,y:0}}));},side);
  const other=side==='L'?'R':'L';assert.ok(Math.max(...trace.map(p=>Math.abs(p['ParamEar'+side])))>.02,side+' moves');
  assert.ok(Math.max(...trace.map(p=>Math.abs(p['ParamEar'+other])))<.0001,other+' stays still');
  for(const k of hair)assert.ok(Math.max(...trace.map(p=>Math.abs(p[k])))<.0001,'ear touch does not move '+k);
  assert.ok(Math.abs(trace[179]['ParamEar'+side])<.02,'ear settles within three seconds');
  fs.writeFileSync(path.join(evidence,'ear-'+side+'-local.json'),JSON.stringify(trace));
 }
 const geometry=await page.evaluate(()=>{r.reset();t=0;step();const b=r.canvas.getBoundingClientRect();return ['L','R'].map(side=>{const p=r.earPoint(side);return {side,hit:r.hitEar(p.x,p.y),p,b:{x:b.x,y:b.y,w:b.width,h:b.height},exclusion:r.exclusion()};});});
 for(const g of geometry)assert.equal(g.hit,g.side,'screen coordinate mapping '+g.side);
 assert.equal(await page.evaluate(()=>r.hitEar(0,650)),null);
 // A real AudioBufferSource → analyser. The gain is zero only to avoid audible test noise.
 await page.evaluate(async()=>{r.reset();t=0;const ctx=new AudioContext();await ctx.resume();window.audioContext=ctx;const b=ctx.createBuffer(1,ctx.sampleRate*.5,ctx.sampleRate);const a=b.getChannelData(0);for(let i=0;i<a.length;i++)a[i]=.22*Math.sin(2*Math.PI*180*i/ctx.sampleRate);const s=ctx.createBufferSource();s.buffer=b;const g=ctx.createGain();g.gain.value=0;s.connect(g);g.connect(ctx.destination);r.setAudioSource(s);s.start();});
 await page.waitForTimeout(100);const loud=await page.evaluate(()=>step(10));assert.ok(loud.ParamMouthOpenY>.1,'actual audio RMS opens mouth');
 await page.waitForTimeout(550);assert.equal(await page.evaluate(()=>sample().ParamMouthOpenY),0,'ended event closes mouth without waiting for a frame');
 await page.evaluate(()=>audioContext.close());
 const lifecycle=await page.evaluate(()=>{r.pokeEar('L');step(30);r.suspend();const paused=sample();step(1);const resumed=sample();r.frame(t+=600000,{look:{x:0,y:0}});return {paused,resumed,afterGap:sample()};});
 for(const state of Object.values(lifecycle)){assert.equal(state.ParamMouthOpenY,0);assert.ok(Math.abs(state.ParamEarL)<.0001,'reset clears ear momentum');}
 const idle=await page.evaluate(()=>{r.reset();t=0;return Array.from({length:2161},()=>step());});
 assert.ok(idle.every(p=>Object.values(p).every(Number.isFinite)));assert.ok(idle.every(p=>p.ParamMouthOpenY===0));
 assert.ok(Math.max(...idle.map(p=>Math.abs(p.ParamEarL)))>.001);assert.ok(Math.max(...idle.map(p=>Math.abs(p.ParamEarR)))>.001);
 assert.ok(idle.some(p=>p.ParamEyeLOpen<.1),'official idle includes blink');
 const longHolds=idle.slice(400,900).filter(p=>Math.abs(p.ParamAngleX-3.5)<.03).length;assert.ok(longHolds>400,'idle has long holds, not continuous pendulum');
 fs.writeFileSync(path.join(evidence,'idle-36s-local.json'),JSON.stringify(idle));
 await page.evaluate(()=>{r.reset();t=0;step();});await page.screenshot({path:path.join(evidence,'V2-render-local.png')});
 const disposal=await page.evaluate(()=>{r.dispose();return r.model===null;});assert.equal(disposal,true);
 assert.deepEqual(errors,[]);assert.ok(requests.filter(u=>u.includes('/runtime/')).every(u=>/[?]v=[^&]+/.test(u)),'all model resources share V2 cache version');
 fs.writeFileSync(path.join(evidence,'renderer-local.json'),JSON.stringify({initial,geometry,physicsGroups:hair,requests,errors,kind:'Mac Chromium, real SDK/model; no phone'},null,2));
 console.log('V2 local model, physics, independent ears, idle, coordinates, actual RMS, lifecycle and disposal passed');
}finally{await browser.close();server.close();}})().catch(e=>{console.error(e);server.close();process.exitCode=1;});

// Real Core, browser decoder and WebGL; native playback clock/transport are simulated.
const {chromium}=require(process.env.PLAYWRIGHT_PATH||'playwright');
const fs=require('node:fs'),http=require('node:http'),path=require('node:path'),assert=require('node:assert/strict');
const root=process.env.FACE_ROOT||__dirname;
const server=http.createServer((req,res)=>{const name=new URL(req.url,'http://local').pathname;
 if(name==='/harness')return res.setHeader('Content-Type','text/html').end('<canvas id="live" style="width:312px;height:536px"></canvas><script src="/vendor/live2dcubismcore.min.js"></script>');
 fs.readFile(path.join(root,name),(e,b)=>e?res.writeHead(404).end():res.setHeader('Content-Type',name.endsWith('.js')?'text/javascript':'application/octet-stream').end(b));
});
(async()=>{await new Promise(r=>server.listen(0,'127.0.0.1',r));const browser=await chromium.launch({headless:true});try{
 const page=await browser.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));await page.goto('http://127.0.0.1:'+server.address().port+'/harness');
 const result=await page.evaluate(async()=>{
  let stream,fetches=0;const clock=Date.now();Date.now=()=>clock;window.EventSource=class {constructor(){stream=this;}close(){this.closed=true;}};
  const originalFetch=window.fetch;window.fetch=(url,...args)=>{if(String(url).includes('/api/speech-audio/')){
   fetches++;const rate=16000,n=rate*2,buffer=new ArrayBuffer(44+n*2),v=new DataView(buffer);const text=(p,s)=>[...s].forEach((c,i)=>v.setUint8(p+i,c.charCodeAt(0)));
   text(0,'RIFF');v.setUint32(4,36+n*2,true);text(8,'WAVEfmt ');v.setUint32(16,16,true);v.setUint16(20,1,true);v.setUint16(22,1,true);v.setUint32(24,rate,true);v.setUint32(28,rate*2,true);v.setUint16(32,2,true);v.setUint16(34,16,true);text(36,'data');v.setUint32(40,n*2,true);
   for(let i=0;i<n;i++)v.setInt16(44+i*2,i<rate/2||i>rate?Math.sin(i*2*Math.PI*220/rate)*12000:0,true);
   return Promise.resolve(new Response(buffer));}return originalFetch(url,...args);};
  const {loadV2}=await import('/live2d-v2.js');const r=await loadV2(document.querySelector('#live'));let t=0;
  const mouth=()=>r.model.parameters.values[r.model.parameters.ids.indexOf('ParamMouthOpenY')];
  const step=()=>{for(let i=0;i<12;i++)r.frame(t+=1000/60);return mouth();};
  step();if(!stream)return {stream:false};
  const state={id:'sample',state:'playing',position:0,clock_at:Date.now()/1000};
  await stream.onmessage({data:JSON.stringify(state)});const loud=step();
  state.position=.7;state.clock_at=Date.now()/1000;stream.onmessage({data:JSON.stringify(state)});const quiet=step();
  state.position=1.2;state.clock_at=Date.now()/1000;stream.onmessage({data:JSON.stringify(state)});const resumed=step();
  stream.onmessage({data:'null'});const ended=mouth();
  state.id='next';await stream.onmessage({data:JSON.stringify(state)});step();stream.onerror();const disconnected=mouth();
  r.suspend();const closed=stream.closed;
  const ctx=new AudioContext(),source=ctx.createOscillator(),gain=ctx.createGain(),sink=ctx.createGain();sink.gain.value=0;gain.gain.value=.3;source.connect(gain);gain.connect(sink);sink.connect(ctx.destination);source.start();await ctx.resume();
  r.setAudioSource(gain);await new Promise(r=>setTimeout(r,60));const callLoud=step();gain.gain.value=0;await new Promise(r=>setTimeout(r,60));const callMuted=step();
  r.setAudioSource(null);const callEnded=mouth();source.stop();await ctx.close();r.dispose();return {stream:true,loud,quiet,resumed,ended,disconnected,closed,fetches,callLoud,callMuted,callEnded};
 });console.log(JSON.stringify(result));assert.equal(result.stream,true,'active V2 must receive native speech clock');assert.ok(result.loud>.2);assert.ok(result.quiet<.06);assert.ok(result.resumed>.2);assert.equal(result.ended,0);assert.equal(result.disconnected,0);assert.equal(result.closed,true);assert.equal(result.fetches,2);assert.ok(result.callLoud>.2);assert.ok(result.callMuted<.06);assert.equal(result.callEnded,0);assert.deepEqual(errors,[]);console.log('PASS native envelope decoded from audio, silence, resume, end, disconnect, disposal',JSON.stringify(result));
 }finally{await browser.close();server.close();}})().catch(e=>{console.error(e);process.exitCode=1;});

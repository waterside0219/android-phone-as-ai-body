// Full client with fake local transport and synthetic audio; no live microphone/provider.
const {chromium}=require(process.env.PLAYWRIGHT_PATH||'playwright');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({headless:true});
 try{
 const page=await browser.newPage({viewport:{width:312,height:536}}),errors=[];
 page.on('pageerror',e=>errors.push(String(e)));
 await page.route('**/*',route=>route.abort());
 await page.setContent(fs.readFileSync(path.join(__dirname,'../face/index.html'),'utf8').replace(/<script[\s\S]*?<\/script>/g,''));
 await page.evaluate(()=>{
  globalThis.mock={requests:[],tracks:[],contexts:[],sources:[],next:[],cancelled:new Set(),mode:'normal',saved:[],clock:Date.now(),seq:0};
  const realSetTimeout=setTimeout;
  window.setTimeout=(fn,ms,...args)=>realSetTimeout(fn,ms===15000?120:ms,...args);
  Object.defineProperty(crypto,'randomUUID',{value:()=> '00000000-0000-4000-8000-'+String(++mock.seq).padStart(12,'0')});
  window.faceMessages={showCallSubtitle:text=>mock.caption=text,clearCallSubtitle:()=>mock.caption=''};
  class Context{
   constructor(){this.sampleRate=16000;this.state='running';this.elapsed=0;this.started=performance.now();this.destination={};this.audioWorklet={addModule:async()=>{}};mock.contexts.push(this);}
   get currentTime(){return this.elapsed+(this.state==='running'?(performance.now()-this.started)/1000:0);}
   createGain(){return {gain:{value:-1},connect(){},disconnect(){}};}
   createMediaStreamSource(){return {connect(){},disconnect(){}};}
   createBuffer(ch,n,rate){return {duration:n/rate,copyToChannel(){},getChannelData:()=>new Float32Array(n)};}
   createBufferSource(){const s={connect(gain){this.gain=gain;},disconnect(){},start(){mock.sources.push(this);},stop(){this.stopped=true;}};return s;}
   async resume(){if(this.state!=='running')this.started=performance.now();this.state='running';this.onstatechange?.();}
   async suspend(){this.elapsed=this.currentTime;this.state='suspended';this.onstatechange?.();}
   async close(){this.state='closed';}
  }
  window.AudioContext=Context;
  window.AudioWorkletNode=class{constructor(){this.port={};mock.capture=this;}connect(){}disconnect(){}};
  Object.defineProperty(navigator,'mediaDevices',{value:{async getUserMedia(){
   if(mock.mode==='late')await new Promise(resolve=>realSetTimeout(resolve,250));
   const track={stop(){this.stopped=true;},applyConstraints:async()=>{},getSettings:()=>({})};mock.tracks.push(track);
   return {getTracks:()=>[track],getAudioTracks:()=>[track]};},enumerateDevices:async()=>[]}});
  window.fetch=async(url,options={})=>{
   const u=new URL(url),data=options.body?JSON.parse(options.body):null;
   mock.requests.push({path:u.pathname,data});
   if(u.pathname==='/api/incoming')return {ok:true,json:async()=>({incoming:null})};
   if(u.pathname==='/api/messages'&&mock.failHistory)throw Error('history temporarily unavailable');
   if(u.pathname==='/api/call-line'){mock.saved.push(data);return {ok:true,json:async()=>({messages:[]})};}
   if(u.pathname.endsWith('/end')){mock.cancelled.add(u.pathname.split('/')[3]);return {ok:true,json:async()=>({remote_confirmed:true})};}
   if(u.pathname==='/api/calls')return {ok:true,json:async()=>({id:data.id})};
   if(u.pathname.endsWith('/events')&&!data){
    await new Promise(resolve=>realSetTimeout(resolve,10));
    let event=mock.next.shift();
    if(!event&&!mock.ready){event={type:'ready'};mock.ready=true;}
    return {ok:true,json:async()=>({events:event?[{data:event}]:[],cursor:1})};
   }
   return {ok:true,json:async()=>({ok:true,messages:[],unread:0})};
  };
 });
 await page.addScriptTag({path:path.join(__dirname,'app.js')});
 await page.evaluate(()=>window.faceCall.dial());
 await page.waitForFunction(()=>faceCall.diagnostic().state==='connected');
 assert.equal(await page.evaluate(()=>faceCall.diagnostic().automatic),true);
 await page.evaluate(()=>{
  const voice=new Float32Array(1600).fill(.1),quiet=new Float32Array(1600);
  for(let i=0;i<3;i++)mock.capture.port.onmessage({data:voice.buffer});
  for(let i=0;i<7;i++)mock.capture.port.onmessage({data:quiet.buffer});
 });
 await page.waitForFunction(()=>mock.requests.some(r=>r.data?.type==='utterance_end'));
 // A complete segment is now required before playback; audio_end still arrives later.
 await page.evaluate(()=>mock.next.push({type:'audio',pcm:btoa('a'.repeat(6400)),audio_sequence:1,generation:1,text:'第一句话',line_id:'line1'},
  {type:'audio_part_end',generation:1,audio_sequence:2,part:1,text:'第一句话',line_id:'line1'}));
 await page.waitForFunction(()=>mock.sources.length===1);
 assert.equal(await page.evaluate(()=>mock.sources[0].gain.gain.value),0,'shared mute applies to call playback');
 await page.evaluate(()=>mock.sources[0].onended());
 const before=await page.evaluate(()=>mock.requests.filter(r=>r.data?.type==='utterance_start').length);
 await page.evaluate(()=>{for(let i=0;i<4;i++)mock.capture.port.onmessage({data:new Float32Array(1600).fill(.1).buffer});});
 await page.waitForTimeout(30);
 assert.equal(await page.evaluate(()=>mock.requests.filter(r=>r.data?.type==='utterance_start').length),before,'mic must remain gated between speech packets');
 await page.evaluate(()=>mock.next.push({type:'audio_end',generation:1,audio_sequence:3}));
 await page.waitForFunction(()=>mock.requests.some(r=>r.data?.type==='played'));
 await page.evaluate(()=>window.dispatchEvent(new CustomEvent('face-settings',{detail:{muted:false,off_mode:false}})));
 await page.evaluate(()=>mock.next.push({type:'audio',pcm:btoa('a'.repeat(6400)),audio_sequence:4,generation:2,text:'晚安',line_id:'line2'},
  {type:'audio_part_end',generation:2,audio_sequence:5,part:1,text:'晚安',line_id:'line2'}));
 await page.waitForFunction(()=>mock.sources.length===2);
 await page.evaluate(()=>mock.contexts[0].suspend());
 await page.waitForTimeout(3300);
 assert.equal(await page.evaluate(()=>!!mock.sources[1].stopped),false,'focus pause does not spend playback timeout');
 await page.evaluate(()=>{mock.contexts[0].resume();mock.sources[1].onended();mock.next.push({type:'audio_end',generation:2,audio_sequence:6});});
 await page.waitForFunction(()=>faceCall.diagnostic().sleeping);
 const sent=await page.evaluate(()=>{mock.failHistory=true;return faceCall.sendText('叫醒你');});
 assert.equal(sent.sent,true,'history failure must not invite sending an already delivered reply again');
 assert.equal(await page.evaluate(()=>faceCall.diagnostic().sleeping),false);
 await page.evaluate(()=>window.dispatchEvent(new CustomEvent('face-settings',{detail:{muted:true,off_mode:true}})));
 await page.waitForFunction(()=>faceCall.diagnostic().state==='idle');
 assert.ok(await page.evaluate(()=>mock.tracks.every(t=>t.stopped)));
 assert.equal(await page.locator('#call-status').isVisible(),true,'terminal status is visible after hangup');
 // An explicit incoming acceptance must work while ordinary face touches are locked.
 await page.evaluate(()=>{mock.ready=false;document.body.classList.add('touch-locked');window.dispatchEvent(new CustomEvent('face-settings',{detail:{muted:true,off_mode:false}}));return faceCall.dial('00000000-0000-4000-8000-999999999999');});
 await page.waitForFunction(()=>faceCall.diagnostic().state==='connected',{},{timeout:1000});
 await page.locator('#call-end').click();
 await page.waitForFunction(()=>faceCall.diagnostic().state==='idle');
 await page.evaluate(()=>document.body.classList.remove('touch-locked'));
 // A permission dialog may finish after the 15 second cancellation.
 await page.evaluate(()=>{mock.mode='late';mock.ready=false;window.dispatchEvent(new CustomEvent('face-settings',{detail:{muted:true,off_mode:false}}));void faceCall.dial();});
 await page.waitForTimeout(400);
 assert.equal(await page.evaluate(()=>faceCall.diagnostic().state),'idle');
 assert.ok(await page.evaluate(()=>mock.tracks.every(t=>t.stopped)),'late microphone grant must release tracks');
 assert.ok(await page.evaluate(()=>mock.contexts.every(c=>c.state==='closed')));
 assert.equal(errors.length,0,errors.join('\n'));
 console.log('Browser call: automatic capture, shared mute, no barge-in between chunks, focus pause/resume, sleep/text wake, full-off cleanup, timeout/late microphone cleanup passed');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});

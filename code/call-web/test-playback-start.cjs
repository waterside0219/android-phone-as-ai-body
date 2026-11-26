// Real client and transport entry, deterministic Web Audio/clock substitutes.
// No microphone, phone, provider, or network requests.
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(process.env.CALL_APP_PATH||path.join(__dirname,'app.js'),'utf8');
const epoch=1793275200000;
async function client(){
  let now=0,poll=null,pollReject=null,sequence=0;
  const timers=new Map(),elements=new Map(),listeners=new Map();
  const model={sources:[],contexts:[],sent:[],logs:[],polls:[],captions:[],resumeCalls:0};
  function element(id){
    if(!elements.has(id))elements.set(id,{hidden:false,textContent:'',handlers:{},
      addEventListener(type,fn){this.handlers[type]=fn;},setAttribute(){},setPointerCapture(){},
      click(){this.handlers.click?.({preventDefault(){}});}});
    return elements.get(id);
  }
  const classes=new Set();
  model.classes=classes;
  const document={getElementById:element,visibilityState:'visible',addEventListener(){},
    body:{classList:{contains:x=>classes.has(x),toggle(x,on){on?classes.add(x):classes.delete(x);}}}};
  class Context{
    constructor(){this.currentTime=0;this.state='running';this.sampleRate=48000;this.destination={};
      this.audioWorklet={addModule:async()=>{}};model.contexts.push(this);}
    createGain(){return {gain:{value:0},connect(){},disconnect(){}};}
    createMediaStreamSource(){return {connect(){},disconnect(){}};}
    createBuffer(ch,length,rate){const samples=new Float32Array(length);return {
      duration:length/rate,length,copyToChannel:data=>samples.set(data),getChannelData:()=>samples};}
    createBufferSource(){return {connect(gain){this.gain=gain;},disconnect(){this.disconnected=true;},
      start(at=0){this.at=at;this.calledAt=now;this.contextState=model.contexts[0].state;model.sources.push(this);},
      stop(){this.stopped=true;timer(()=>{if(!this.ended){this.ended=true;this.onended?.();}},0,false);}};}
    async resume(){model.resumeCalls++;if(model.resumeError)throw Error('resume blocked');if(model.resumeWait)await model.resumeWait;
      if(this.state==='closed')throw Error('closed');this.state='running';this.onstatechange?.();}
    async suspend(){this.state='suspended';this.onstatechange?.();}
    async close(){this.state='closed';}
    getOutputTimestamp(){return {contextTime:this.currentTime,performanceTime:now};}
  }
  function timer(fn,delay,repeat){const id=++sequence;timers.set(id,{fn,at:now+delay,delay,repeat});return id;}
  const sandbox={document,console:{info:(...args)=>model.logs.push(args),log(){},warn(){}},
    Date:class extends Date{constructor(...args){super(...(args.length?args:[epoch+now]));}static now(){return epoch+now;}},
    performance:{now:()=>now},crypto:{randomUUID:()=>String(++sequence)},
    setTimeout:(fn,ms)=>timer(fn,ms,false),setInterval:(fn,ms)=>timer(fn,ms,true),
    clearTimeout:id=>timers.delete(id),clearInterval:id=>timers.delete(id),
    Uint8Array,Int16Array,Float32Array,DataView,AbortController,
    AbortSignal:{timeout:()=>new AbortController().signal,any:signals=>signals[0]},
    atob:x=>Buffer.from(x,'base64').toString('binary'),btoa:x=>Buffer.from(x,'binary').toString('base64'),
    Blob:class{},URL:{createObjectURL:()=>'',revokeObjectURL(){}},
    CustomEvent:class{constructor(type,options={}){this.type=type;Object.assign(this,options);}},
    AudioContext:Context,AudioWorkletNode:class{constructor(){this.port={};model.capture=this.port;}connect(){}disconnect(){}},
    faceMessages:{showCallSubtitle(text,progress){model.captions.push({text,progress,at:now});},clearCallSubtitle(){model.captions.push({text:'',at:now});}},
    navigator:{mediaDevices:{getUserMedia:async()=>({getTracks:()=>[],getAudioTracks:()=>[{}]}),enumerateDevices:async()=>[]}},
    addEventListener:(type,fn)=>listeners.set(type,fn),dispatchEvent:event=>listeners.get(event.type)?.(event),
    fetch:async(url,options={})=>{
      const data=options.body?JSON.parse(options.body):null;
      if(url.includes('/events?')){model.polls.push(url);return new Promise((resolve,reject)=>{poll=resolve;pollReject=reject;});}
      if(data)model.sent.push(data);
      if(data&&model.transport)await model.transport(data);
      if(model.ackWait&&data?.type==='audio_ack')await model.ackWait;
      return {ok:true,json:async()=>url.endsWith('/api/incoming')?{incoming:null}:{ok:true,remote_confirmed:true}};
    }};
  sandbox.window=sandbox;vm.createContext(sandbox);vm.runInContext(source,sandbox);
  const flush=async()=>{for(let i=0;i<40;i++)await Promise.resolve();};
  async function deliver(...events){await flush();assert.ok(poll,'client is polling');
    const resolve=poll;poll=null;resolve({ok:true,json:async()=>({events:events.map(data=>({data})),cursor:++sequence})});await flush();}
  async function advance(ms){
    const target=now+ms;
    while(now<target){
      const next=Math.min(target,...Array.from(timers.values(),t=>t.at));
      const step=next-now;now=next;
      for(const ctx of model.contexts)if(ctx.state==='running')ctx.currentTime+=step/1000;
      for(const s of model.sources)if(!s.stopped&&!s.loop&&!s.ended&&model.contexts[0].state==='running'&&model.contexts[0].currentTime+1e-9>=s.at+s.buffer.duration){s.ended=true;s.onended?.();}
      for(const [id,t]of [...timers])if(t.at<=now){if(t.repeat)t.at+=t.delay;else timers.delete(id);t.fn();}
      await flush();
    }
  }
  await sandbox.faceCall.dial();await deliver({type:'ready'});
  return {model,ctx:model.contexts[0],api:sandbox.faceCall,deliver,advance,flush,
    mute:value=>sandbox.dispatchEvent(new sandbox.CustomEvent('face-settings',{detail:{muted:value}})),
    hangup:async()=>{element('call-end').click();await flush();},
    rejectPoll:async()=>{assert.ok(pollReject);poll=null;pollReject(Error('network lost'));await flush();},
    status:()=>element('call-status').textContent,
    capture:values=>model.capture.onmessage({data:Float32Array.from(values).buffer}),
    now:()=>now,diagnostics:()=>JSON.parse(JSON.stringify(sandbox.faceCall.diagnostic(true)))};
}
let seq=0;
function audio(generation=1,ms=512){const pcm=Buffer.alloc(ms*64);pcm.writeInt16LE(12345,0);pcm.writeInt16LE(-23456,pcm.length-2);
  return {type:'audio',generation,audio_sequence:++seq,pcm:pcm.toString('base64')};}
const part=generation=>({type:'audio_part_end',generation,audio_sequence:++seq,part:1});
const end=generation=>({type:'audio_end',generation,audio_sequence:++seq});
function approx(actual,expected,label){assert.ok(Math.abs(actual-expected)<1e-6,`${label}: ${actual} != ${expected}`);}
async function measure(){
  const c=await client();await c.deliver(audio(),part(1),end(1));await c.advance(1000);await c.advance(31000);
  await c.ctx.suspend();const resumes=c.model.resumeCalls,arrived=c.now();await c.deliver(audio(2),end(2));
  const suspended=c.model.sources.at(-1);
  const sleep={arrival_ms:arrived,context_state:suspended?.contextState,resume_calls:c.model.resumeCalls-resumes,
    lead_ms:suspended?(suspended.at-c.ctx.currentTime)*1000:null};
  await c.ctx.resume();await c.advance(1000);await c.hangup();
  const late=await client();await late.deliver({type:'playback',generation:1});await late.advance(2000);
  const before=late.model.sources.length;await late.deliver(audio(),end(1));await late.advance(200);
  const first=late.model.sources[0];
  console.log(JSON.stringify({kind:'SIMULATED',sleep,late:{first_packet_delay_ms:2000,sources_before_packet:before,
    scheduled_start_ms:first.at*1000,sample_count:first.buffer.length,first_sample:first.buffer.getChannelData(0)[0]},
    diagnostics:late.diagnostics()},null,2));await late.hangup();
}
const cases={
  async 'suspended restart resumes before scheduling with 150ms lead'(){
    const c=await client();try{
      await c.deliver(audio(),end(1));await c.advance(1000);await c.advance(31000);await c.ctx.suspend();
      const resumes=c.model.resumeCalls;await c.deliver(audio(2),end(2));
      const s=c.model.sources.at(-1);assert.equal(c.model.resumeCalls,resumes+1,'resume before speech');
      assert.equal(s.contextState,'running');approx(s.at-c.ctx.currentTime,.15,'cold lead');
    }finally{await c.hangup();}
  },
  async 'running but idle output gets 150ms, contiguous packets stay gapless'(){
    const c=await client();try{
      await c.deliver(audio(),audio(),part(1),audio(),end(1));await c.advance(1100);
      assert.equal(c.model.sources.length,3);
      approx(c.model.sources[0].at,.15,'first lead');
      for(let i=1;i<3;i++)approx(c.model.sources[i].at,c.model.sources[i-1].at+.512,'gapless across packets/part boundary');
      await c.advance(32000);const at=c.ctx.currentTime;await c.deliver(audio(2),end(2));
      approx(c.model.sources.at(-1).at-at,.15,'long idle lead');
    }finally{await c.hangup();}
  },
  async 'late complete first packet preserves every sample and records arrival versus start'(){
    const c=await client();try{
      await c.deliver({type:'playback',generation:1});await c.advance(2000);assert.equal(c.model.sources.length,0);
      const packet=audio();await c.deliver(packet,packet,part(1),end(1));await c.advance(200);
      assert.equal(c.model.sources.length,1,'duplicate does not play twice');
      const s=c.model.sources[0];assert.equal(s.buffer.length,16384);
      approx(s.buffer.getChannelData(0)[0],12345/32768,'first sample');
      approx(s.buffer.getChannelData(0).at(-1),-23456/32768,'last sample');
      const rows=c.diagnostics().playback;assert.equal(rows?.length,1,'one timing row per sentence');
      const row=rows[0];assert.equal(row.first_packet_at_ms,epoch+2000);assert.equal(row.first_packet_context_state,'running');
      assert.equal(row.playback_to_first_packet_ms,2000);approx(row.scheduled_start_context_s,2.15,'late first schedule');
      assert.ok(row.start_observed_at_ms>=epoch+2150&&row.start_observed_at_ms<=epoch+2190,'observe rendering, not just scheduling');
      assert.equal(row.gap_since_previous_end_ms,null,'no previous sentence');
      assert.ok(!JSON.stringify(rows).includes(packet.pcm),'no audio in diagnostics');
    }finally{await c.hangup();}
  },
  async 'each sentence records previous ending and initial suspended state'(){
    const c=await client();try{
      await c.deliver(audio(),part(1),end(1));await c.advance(1000);await c.advance(31000);
      await c.ctx.suspend();await c.deliver(audio(2),end(2));await c.advance(200);
      const rows=c.diagnostics().playback;assert.equal(rows?.length,2);
      assert.equal(rows[1].first_packet_context_state,'suspended');
      assert.ok(rows[1].gap_since_previous_end_ms>=31000,'wall gap survives suspended audio clock');
      assert.ok(rows[1].start_observed_at_ms>rows[1].first_packet_at_ms);
    }finally{await c.hangup();}
  },
  async 'hangup during resume never starts a late first packet'(){
    const c=await client();try{
      await c.ctx.suspend();let release;c.model.resumeWait=new Promise(resolve=>release=resolve);
      const resumes=c.model.resumeCalls;
      await c.deliver(audio(),end(1));assert.equal(c.model.resumeCalls,resumes+1,'complete segment reaches resume');assert.equal(c.model.sources.length,0,'must await resume');
      await c.hangup();release();await c.flush();assert.equal(c.model.sources.length,0);
    }finally{await c.hangup();}
  },
  async 'blocked automatic resume keeps the first packet for manual continue'(){
    const c=await client();try{
      await c.ctx.suspend();c.model.resumeError=true;await c.deliver(audio(),end(1));await c.advance(5000);
      assert.equal(c.model.sources.length,1);assert.ok(!c.model.sources[0].stopped);
      assert.equal(c.diagnostics().playback[0].start_observed_at_ms,null);
      c.model.resumeError=false;await c.ctx.resume();await c.advance(200);
      assert.ok(c.diagnostics().playback[0].start_observed_at_ms>=epoch+5150);
      approx(c.model.sources[0].buffer.getChannelData(0)[0],12345/32768,'retained first sample');
    }finally{await c.hangup();}
  },
  async 'short first packet is observed and detailed history is opt-in and bounded'(){
    const c=await client();try{
      await c.deliver(audio(1,50),end(1));await c.advance(250);
      assert.ok(c.diagnostics().playback[0].start_observed_at_ms,'short source still records rendering');
      assert.equal(c.api.diagnostic().playback,undefined,'routine face polling must not copy timing history');
      for(let i=2;i<=62;i++){await c.deliver(audio(i,50),end(i));await c.advance(250);}
      const rows=c.diagnostics().playback;assert.equal(rows.length,60);assert.equal(rows[0].generation,3);
      rows[0].generation=-1;assert.equal(c.diagnostics().playback[0].generation,3,'diagnostics cannot change stored evidence');
    }finally{await c.hangup();}
  },
  async 'shared mute, pause, and hangup retain original protections'(){
    const c=await client();try{
      await c.deliver(audio(1,1000),end(1));assert.equal(c.model.sources[0].gain.gain.value,0);
      c.mute(false);assert.equal(c.model.sources[0].gain.gain.value,1);await c.advance(200);await c.ctx.suspend();
      await c.advance(5000);assert.ok(!c.model.sources[0].stopped,'suspended time does not consume timeout');
      await c.ctx.resume();await c.advance(100);await c.hangup();assert.ok(c.model.sources.every(s=>s.stopped));
    }finally{await c.hangup();}
  }
};
module.exports={client,audio,part,end,approx,epoch};
if(require.main===module)(async()=>{
  if(process.env.G1_MEASURE)return measure();
  let failures=0;
  for(const [name,run]of Object.entries(cases))try{await run();console.log('PASS',name);}catch(e){failures++;console.error('FAIL',name,'\n',e.stack);}
  console.log(`${Object.keys(cases).length-failures}/${Object.keys(cases).length} simulated playback checks passed`);if(failures)process.exitCode=1;
})().catch(e=>{console.error(e);process.exitCode=1;});

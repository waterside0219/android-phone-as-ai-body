'use strict';

class VoiceGate {
  constructor(emit,onVoice=()=>{}) {this.emit=emit;this.onVoice=onVoice;this.pre=new Int16Array();this.pending=new Int16Array();this.id=null;}
  feed(samples,forced=false) {
    if(!samples.length)return;
    const voiced=Math.sqrt(samples.reduce((sum,n)=>sum+(n/32768)**2,0)/samples.length)>=.015;
    if(voiced)this.onVoice();
    if(!this.id){
      if(!voiced&&!forced){const joined=new Int16Array(this.pre.length+samples.length);joined.set(this.pre);joined.set(samples,this.pre.length);this.pre=joined.slice(-5440);return;}
      this.id=crypto.randomUUID();this.total=0;this.voiced=0;this.quiet=0;
      this.emit({type:'utterance_start',id:this.id});this.append(this.pre);this.pre=new Int16Array();
    }
    const length=Math.min(samples.length,480000-this.total);
    if(voiced||forced){this.voiced+=length;this.quiet=0;}else this.quiet+=length;
    this.append(samples.subarray(0,length));
    if(this.total>=480000||(!forced&&this.quiet>=11200))this.finish();
  }
  append(samples){
    this.total+=samples.length;
    const joined=new Int16Array(this.pending.length+samples.length);joined.set(this.pending);joined.set(samples,this.pending.length);
    let offset=0;while(joined.length-offset>=1600){this.packet(joined.subarray(offset,offset+1600));offset+=1600;}
    this.pending=joined.slice(offset);
  }
  packet(samples){
    const bytes=new Uint8Array(samples.length*2),view=new DataView(bytes.buffer);
    samples.forEach((value,index)=>view.setInt16(index*2,value,true));
    this.emit({type:'utterance_audio',id:this.id,pcm:btoa(String.fromCharCode(...bytes))});
  }
  finish(discard=false){
    if(this.id){if(this.pending.length)this.packet(this.pending);this.emit({type:'utterance_end',id:this.id,discard:discard||this.voiced<4000});}
    this.id=null;this.pending=new Int16Array();this.pre=new Int16Array();
  }
}
function resample16k(samples,rate,state){
  let values=samples;
  if(rate!==16000){
    const joined=new Float32Array((state.buffer?.length||0)+samples.length);joined.set(state.buffer||[]);joined.set(samples,state.buffer?.length||0);
    const result=[];let position=state.position||0;
    while(position<joined.length-1){const left=Math.floor(position),fraction=position-left;result.push(joined[left]*(1-fraction)+joined[left+1]*fraction);position+=rate/16000;}
    const consumed=Math.min(Math.floor(position),joined.length);state.buffer=joined.slice(consumed);state.position=position-consumed;values=result;
  }
  return Int16Array.from(values,value=>Math.round(Math.max(-1,Math.min(1,value))*(value<0?32768:32767)));
}
if(typeof module!=='undefined')module.exports={VoiceGate,resample16k};
else (()=>{
  const BASE='http://127.0.0.1:8191',FACE='http://127.0.0.1:8192';
  const $=id=>document.getElementById(id);
  let current=null,invitation=null,muted=true,off=false,incomingTimer,statusUntil=0,lastBusy=false;
  const active=s=>current===s&&!s.ended;
  const status=text=>{$('call-status').textContent=text;statusUntil=Date.now()+8000;$('call-status').hidden=false;};
  async function json(path,body,timeout=6000,signal=null){
    const signals=[AbortSignal.timeout(timeout)];if(signal)signals.push(signal);
    const response=await fetch(path.startsWith('http')?path:BASE+path,{method:body===undefined?'GET':'POST',
      headers:body===undefined?{}:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body),signal:AbortSignal.any(signals),cache:'no-store'});
    const data=await response.json();if(!response.ok){const error=Error(data.error||'连接暂时不可用');error.status=response.status;throw error;}return data;
  }
  function controls(){
    const busy=!!current||!!invitation;
    document.body.classList.toggle('call-active',busy);$('call-controls').hidden=!busy;
    document.body.classList.toggle('call-attention',!!(current?.recovering||current?.buffering));
    $('call-status').hidden=!busy&&Date.now()>statusUntil;$('call-answer').hidden=!invitation||!!current;
    $('call-end').textContent=invitation&&!current?'拒接':current?.ready?'挂断':'取消';
    $('call-ptt-mode').hidden=!current?.ready;$('call-mute').hidden=!current?.ready;
    $('call-ptt-mode').textContent=current?.manual?'按住说话':'自动听';
    $('call-ptt-mode').setAttribute('aria-pressed',String(current?.manual===true));
    $('call-mute').textContent=muted?'声音：关':'声音：开';
    $('call-resume').hidden=!current?.ctx||current.ctx.state==='running';
    if(busy&&!lastBusy)window.dispatchEvent(new CustomEvent('face-call-active'));lastBusy=busy;
  }
  function recovery(s,reason,on){
    if(!active(s))return;
    const reasons=s.recoveryReasons||(s.recoveryReasons=new Set()),was=s.recovering;
    if(on)reasons.add(reason);else reasons.delete(reason);s.recovering=reasons.size>0;
    if(s.recovering&&!was){s.gate.finish(true);stopBreathing(s);}
    if(s.recovering)status('连接中断，正在自动重连… 暂时不能说话');
    else if(was&&!s.recovering)status('已重新接通 · '+(s.manual?'按住脸说话':'自动听'));
    controls();
  }
  const retryable=error=>!error.status||error.status===408||error.status===429||error.status>=500;
  function delay(s,ms){return new Promise(resolve=>{const done=()=>{clearTimeout(timer);s.abort.signal.removeEventListener('abort',done);resolve();};
    const timer=setTimeout(done,ms);s.abort.signal.addEventListener('abort',done,{once:true});if(s.abort.signal.aborted)done();});}
  function send(s,message){
    if(!active(s))return Promise.reject(Error('通话已结束'));
    const audioAck=message.type==='audio_ack';
    if(audioAck){s.latestAudioAck=Math.max(s.latestAudioAck||0,message.audio_sequence);if(s.audioAckQueued)return Promise.resolve();s.audioAckQueued=true;}
    if(++s.pending>60){void finish(s,'上传跟不上，通话已结束');return Promise.reject(Error('上传队列已满'));}
    const result=s.sending.then(async()=>{
      if(audioAck){message={...message,audio_sequence:s.latestAudioAck};s.audioAckQueued=false;}
      if(s.discardedUploads?.has(message.id)){
        if(message.type==='utterance_end')message={...message,discard:true};
        else if(['utterance_start','utterance_audio'].includes(message.type))return;
      }
      const started=performance.now();
      while(active(s)){
        if(performance.now()-started>=95000)throw Error('连接中断太久，未能重连，请重新拨号');
        try{const data=await json(`/api/calls/${s.id}/events`,message,Math.min(6000,95000-(performance.now()-started)),s.abort.signal);
          if(message.type==='utterance_end')s.discardedUploads?.delete(message.id);
          recovery(s,'send',false);return data;}
        catch(error){
          if(!active(s)||!retryable(error))throw error;
          const capture=['utterance_start','utterance_audio'].includes(message.type);
          if(capture)(s.discardedUploads||(s.discardedUploads=new Set())).add(message.id);
          recovery(s,'send',true);
          // Streaming capture has no per-packet identity: never duplicate an uncertain upload.
          // recovery() closes that utterance with an idempotent discard instead.
          if(capture)return;
          if(performance.now()-started>=95000)throw Error('连接中断太久，未能重连，请重新拨号');
          await delay(s,Math.min(1000,95000-(performance.now()-started)));
        }
      }
      throw Error('通话已结束');
    });
    // 400 是"这句没收下"（如上一句仍在处理中），只丢掉这句并提示，不挂电话。
    s.sending=result.catch(error=>{if(!active(s))return;if(['audio_ack','played_part','ping','media_ready'].includes(message.type))return; // 小回执发不出去就算了
      if(error.status===400){if(message.id)(s.discardedUploads||(s.discardedUploads=new Set())).add(message.id);status(error.message||'这句话没送出，请再说一遍');return;}void finish(s,error.message);}).finally(()=>s.pending--);
    return result;
  }
  function wake(s){const wasSleeping=s.sleeping;s.lastSound=Date.now();s.sleepRequested=false;s.sleeping=false;stopBreathing(s);if(s.ready&&wasSleeping)status('已接通 · '+(s.manual?'按住脸说话':'自动听'));}
  function stopBreathing(s){if(s.breath){try{s.breath.stop();}catch{}s.breath.disconnect();s.breath=null;}}
  function breathe(s){
    if(!active(s)||!s.ready||s.busy||s.breath||!s.ctx)return;
    s.sleeping=true;status('睡眠中 · 说话或打字叫醒');
    const length=s.ctx.sampleRate*6,buffer=s.ctx.createBuffer(1,length,s.ctx.sampleRate),data=buffer.getChannelData(0);
    let low=0;for(let i=0;i<length;i++){const t=i/s.ctx.sampleRate;low=.94*low+.06*(Math.random()*2-1);const phase=t<1.8?t/1.8:t<4?(4-t)/2.2:0;data[i]=low*.05*Math.sin(Math.max(0,phase)*Math.PI/2)**2;}
    s.breath=s.ctx.createBufferSource();s.breath.buffer=buffer;s.breath.loop=true;s.breath.connect(s.gain);s.breath.start();
  }
  function microphoneFrame(s,buffer){
    if(!active(s)||!s.ready||s.busy||s.recovering||(s.manual&&!s.holding)||s.ctx.state!=='running')return;
    s.gate.feed(resample16k(new Float32Array(buffer),s.ctx.sampleRate,s.resampler),s.holding);
  }
  async function setupAudio(s){
    const Context=window.AudioContext||window.webkitAudioContext;
    if(!Context)throw Error('浏览器没有可用音频');
    const ctx=s.ctx=new Context();
    s.gain=ctx.createGain();s.gain.gain.value=muted?0:1;s.gain.connect(ctx.destination);
    await ctx.resume();if(!active(s))return;
    const stream=await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:true,noiseSuppression:true,autoGainControl:true},video:false});
    if(!active(s)){stream.getTracks().forEach(track=>track.stop());return;}
    s.mic=stream;
    const devices=await navigator.mediaDevices.enumerateDevices();if(!active(s))return;
    const phone=devices.find(device=>device.kind==='audioinput'&&/built.?in|内置|手机麦/i.test(device.label));
    const bluetooth=devices.find(device=>device.kind==='audiooutput'&&/bluetooth|蓝牙/i.test(device.label));
    const track=stream.getAudioTracks()[0];
    if(phone){try{await track.applyConstraints({deviceId:{exact:phone.deviceId}});s.micPinned=track.getSettings().deviceId===phone.deviceId;}catch{}}
    if(bluetooth){await track.applyConstraints({echoCancellation:false});s.bluetooth=true;if(typeof ctx.setSinkId==='function'){await ctx.setSinkId(bluetooth.deviceId);s.outputSelected=true;}}
    if(!active(s))return;
    s.worklet=URL.createObjectURL(new Blob([`class Capture extends AudioWorkletProcessor{process(inputs){if(inputs[0]?.[0])this.port.postMessage(inputs[0][0].slice().buffer);return true;}}registerProcessor('face-capture',Capture);`],{type:'text/javascript'}));
    await ctx.audioWorklet.addModule(s.worklet);if(!active(s))return;
    s.source=ctx.createMediaStreamSource(stream);s.capture=new AudioWorkletNode(ctx,'face-capture');
    s.capture.port.onmessage=event=>microphoneFrame(s,event.data);
    s.silent=ctx.createGain();s.silent.gain.value=0;s.source.connect(s.capture);s.capture.connect(s.silent);s.silent.connect(ctx.destination);
    ctx.onstatechange=()=>{if(active(s)){if(ctx.state!=='running'){s.gate.finish(true);status('声音已暂停 · 点继续');}controls();}};
    track.onended=()=>{if(active(s))void finish(s,'麦克风已停止，通话结束');};
  }
  async function cleanup(s){
    clearTimeout(s.deadline);clearInterval(s.sleepTimer);s.abort.abort();stopBreathing(s);
    if(s.playing){s.playing.finish();s.playing=null;}
    window.faceLive2D?.setAudioSource(null);
    if(s.capture){s.capture.port.onmessage=null;s.capture.disconnect();}
    s.source?.disconnect();s.silent?.disconnect();s.gain?.disconnect();
    if(s.mic)s.mic.getTracks().forEach(track=>{track.onended=null;track.stop();});
    if(s.worklet)URL.revokeObjectURL(s.worklet);
    if(s.ctx){s.ctx.onstatechange=null;if(s.ctx.state!=='closed')try{await s.ctx.close();}catch{}}
  }
  async function finish(s,message='已挂断',notify=true){
    if(!s||s.ended)return;s.ended=true;
    if(current===s){current=null;window.faceMessages?.clearCallSubtitle();controls();status(message);}
    await cleanup(s);
    if(notify){try{const result=await json(`/api/calls/${s.id}/end`,{},6000);if(!result.remote_confirmed&&!current)status('本机已挂断，远端挂断待确认');}catch{if(!current)status('本机已挂断，远端挂断待确认');}}
  }
  async function dial(incomingId=null){
    if(current||off||(!incomingId&&document.body.classList.contains('touch-locked')))return;
    const s={id:incomingId||crypto.randomUUID(),abort:new AbortController(),ready:false,ended:false,manual:false,holding:false,ctx:null,mic:null,
      sending:Promise.resolve(),pending:0,queue:[],queueBytes:0,draining:false,busy:false,lastSound:Date.now(),lastPacket:0,resampler:{},generations:new Set(),seen:new Set(),lines:new Map()};
    s.gate=new VoiceGate(message=>{void send(s,message).catch(()=>{});},()=>wake(s));current=s;invitation=null;controls();status('正在接通… 下划可取消');
    s.deadline=setTimeout(()=>{if(active(s)&&!s.ready)void finish(s,'15秒未接通，已取消');},15000);
    try{
      await Promise.all([setupAudio(s),json('/api/calls',{id:s.id},15000,s.abort.signal)]);if(!active(s))return;
      void poll(s);
      s.sleepTimer=setInterval(()=>{
        if(!active(s))return;
        const waiting=s.queue[0]?.playbackPart;
        if(waiting&&!waiting.complete&&Date.now()-waiting.timing.first_packet_at_ms>=1000&&!s.playing){s.buffering=true;if(!s.recovering)status('网络慢，正在缓冲声音…');controls();}
        if(s.busy&&!s.playing&&!s.queue.length&&Date.now()-s.lastPacket>15000){s.busy=false;s.generations.clear();}
        if(!s.busy&&((s.sleepRequested&&Date.now()-s.lastSound>5000)||((new Date().getHours()>=23||new Date().getHours()<7)&&Date.now()-s.lastSound>=600000)))breathe(s);
      },1000);
    }catch(error){if(active(s))await finish(s,error.name==='NotAllowedError'?'麦克风未允许，已取消':error.message||'未接通');}
  }
  async function saveLine(s,speaker,text,id){
    if(!text?.trim())return;
    const identifier=s.id+':'+(id||crypto.randomUUID());
    if(s.lines.get(identifier)===text)return;s.lines.set(identifier,text);if(s.lines.size>60)s.lines.delete(s.lines.keys().next().value);
    if(/晚安|睡觉吧|睡吧/.test(text))s.sleepRequested=true;
    try{await json(FACE+'/api/call-line',{id:identifier,speaker,text});}catch{if(active(s))status('通话继续，文字未保存');}
  }
  function renderCaption(s,part){
    if(!active(s)||!part||part.timing.scheduled_start_context_s===null)return;
    const elapsed=s.ctx.currentTime-part.timing.scheduled_start_context_s;
    if(elapsed<0)return;
    if(elapsed>=part.duration){if(s.captionPart===part){window.faceMessages?.showCallSubtitle('',0);s.captionPart=null;}return;}
    if(!part.cues?.length)return;
    const cue=part.cues.find(cue=>elapsed<cue.end)||part.cues.at(-1);
    s.captionPart=part;window.faceMessages?.showCallSubtitle(cue.text,Math.max(0,Math.min(1,(elapsed-cue.start)/(cue.end-cue.start))));
  }
  function completePart(s,part,message){
    if(!part)return;part.complete=true;
    const prefixes=s.captionPrefixes||(s.captionPrefixes=new Map()),previous=prefixes.get(message.generation)||'',full=message.text||'';
    const text=(full.startsWith(previous)?full.slice(previous.length):full).trim();
    if(full)prefixes.set(message.generation,full);
    const chunks=text.match(/[^，,。！？!?；;：:\n]+[，,。！？!?；;：:\n]*/gu)||[];
    const weights=chunks.map(text=>Array.from(text.replace(/\s/gu,'')).length),total=weights.reduce((a,b)=>a+b,0);
    let at=0;part.cues=chunks.map((text,i)=>{const start=at;at+=part.duration*weights[i]/total;return {text:text.trim(),start,end:at};});
  }
  async function whenPlayed(s,at){
    while(active(s)&&s.tail&&s.ctx.currentTime<at)await delay(s,40);
  }
  async function playPacket(s,packet){
    const binary=atob(packet.pcm);if(!binary.length||binary.length%2||binary.length>32000*2*60)throw Error('无效语音片段');
    const bytes=Uint8Array.from(binary,c=>c.charCodeAt(0)),view=new DataView(bytes.buffer),samples=new Float32Array(bytes.length/2);
    for(let i=0;i<samples.length;i++)samples[i]=view.getInt16(i*2,true)/32768;
    const ctx=s.ctx;
    if(ctx.state!=='running'){try{await ctx.resume();}catch{/* Keep the packet queued on the paused clock for the Continue button. */}}
    if(!active(s))return;
    const buffer=ctx.createBuffer(1,samples.length,32000);buffer.copyToChannel(samples,0);
    const source=ctx.createBufferSource();source.buffer=buffer;source.connect(s.gain);window.faceLive2D?.setAudioSource(s.gain);stopBreathing(s);s.lastSound=Date.now();
    if(packet.text)void saveLine(s,'AI',packet.text,packet.line_id);
    // Queue each packet onto the previous one's end time so 16 KiB packets join without a gap; return shortly before it ends so the next one is decoded in time.
    // Only an empty output timeline needs the 150ms wake-up lead; packet/part boundaries remain gapless.
    const lead=packet.playbackTiming?.scheduled_start_context_s===null&&(!s.tail||s.tail<=ctx.currentTime)?0.15:0.03;
    const at=Math.max(ctx.currentTime+lead,s.tail||0),end=at+buffer.duration;s.tail=end;
    const timing=packet.playbackTiming,first=timing?.scheduled_start_context_s===null;
    if(first){timing.scheduled_at_ms=Date.now();timing.scheduled_start_context_s=at;timing.schedule_context_state=ctx.state;}
    const observeStart=()=>{
      if(!first||timing.start_observed_at_ms!==null||ctx.state!=='running'||ctx.currentTime<at)return;
      timing.start_observed_at_ms=Date.now();timing.start_observed_context_s=ctx.currentTime;
      // Browser rendering evidence, not a measurement of sound at the physical speaker.
      const stamp=ctx.getOutputTimestamp?.();
      if(stamp?.contextTime>0)timing.output_start_estimate_at_ms=Date.now()-performance.now()+stamp.performanceTime+(at-stamp.contextTime)*1000;
      console.info('call-playback',JSON.stringify(timing));
    };
    const sources=s.sources||(s.sources=new Set());sources.add(source);
    const stopAll=()=>{for(const x of sources){try{x.stop();}catch{}x.disconnect();}sources.clear();s.tail=0;s.playing=null;window.faceLive2D?.setAudioSource(null);};
    source.onended=()=>{observeStart();renderCaption(s,packet.playbackPart);sources.delete(source);source.disconnect();if(!sources.size){window.faceLive2D?.setAudioSource(null);s.lastOutputEndAt=Date.now();if(s.playing?.finish===stopAll)s.playing=null;}};
    s.playing={source,finish:stopAll};source.start(at);
    await new Promise(resolve=>{
      let elapsed=0,last=performance.now(),released=false;
      const timer=setInterval(()=>{const now=performance.now();if(ctx.state==='running')elapsed+=now-last;last=now;
        if(!active(s)){clearInterval(timer);resolve();return;}
        observeStart();renderCaption(s,packet.playbackPart);
        if(ctx.currentTime>=end-.25){if(!released){released=true;resolve();}if(ctx.currentTime>=end)clearInterval(timer);}
        else if(elapsed>(buffer.duration+3)*1000){clearInterval(timer);stopAll();resolve();}},40);
    });
  }
  async function drain(s){
    if(s.draining)return;s.draining=true;
    try{
      while(active(s)&&s.queue.length){
        if(s.queue[0].type==='audio'&&!s.queue[0].playbackPart.complete)break;
        if(s.buffering){s.buffering=false;if(!s.recovering)status('已接通 · '+(s.manual?'按住脸说话':'自动听'));controls();}
        const packet=s.queue.shift();s.queueBytes-=packet.pcm?.length||0;
        if(packet.type==='audio'){
          try{await playPacket(s,packet);}catch{status('跳过一段未能播放的声音');}
        }else if(packet.type==='audio_part_end'){
          void saveLine(s,'AI',packet.text,packet.line_id);
          void whenPlayed(s,s.tail||0).then(()=>{if(active(s))return send(s,{type:'played_part',part:packet.part,generation:packet.generation,id:packet.line_id,text:packet.text});}).catch(()=>{});
        }else if(packet.type==='audio_end'){
          void whenPlayed(s,s.tail||0).then(()=>{if(!active(s))return;s.generations.delete(packet.generation);s.busy=s.generations.size>0;s.lastSound=Date.now();
            if(s.sleepRequested&&!s.busy)breathe(s);return send(s,{type:'played',generation:packet.generation});}).catch(()=>{});
        }
      }
    }catch(error){if(active(s))status(error.message);}
    finally{s.draining=false;if(active(s)&&!s.queue.length){s.busy=s.generations.size>0;s.lastSound=Date.now();if(s.sleepRequested&&!s.busy)breathe(s);}}
  }
  function discardIncompletePart(s){
    const failed=s.receivingPart;if(!failed)return;s.receivingPart=null;
    s.queue=s.queue.filter(packet=>packet.playbackPart!==failed);s.queueBytes=s.queue.reduce((sum,packet)=>sum+(packet.pcm?.length||0),0);
    s.generations.delete(failed.generation);s.captionPrefixes?.delete(failed.generation);
    s.buffering=false;s.busy=s.generations.size>0||!!s.playing;controls();void drain(s);
  }
  function handle(s,message){
    if(message.type==='ready'){
      s.ready=true;clearTimeout(s.deadline);recovery(s,'upstream',false);void send(s,{type:'media_ready'}).catch(()=>{});window.faceMessages?.showCallSubtitle('',0);if(!s.recovering)status('已接通 · 自动听'+(s.micPinned?'':' · 系统麦'));controls();return;
    }
    if(message.type==='playback'){
      if(s.receivingPart&&s.receivingPart.generation!==message.generation)discardIncompletePart(s);
      s.busy=true;if(Number.isInteger(message.generation))s.generations.add(message.generation);s.lastPacket=Date.now();s.playbackNotice={generation:message.generation,at:s.lastPacket};s.gate.finish(true);return;
    }
    if(message.type==='interrupt')return; // Barge-in stays disabled; queued speech is never cut off.
    if(['audio','audio_part_end','audio_end'].includes(message.type)){
      if(!Number.isInteger(message.audio_sequence))return;
      const key=message.generation+':'+message.audio_sequence;if(s.seen.has(key))return;
      s.seen.add(key);if(s.seen.size>1000)s.seen.delete(s.seen.values().next().value);
      if(message.type==='audio'){
        if(!s.receivingPart||s.receivingPart.generation!==message.generation){
          const arrival=Date.now(),previousEnd=(s.draining||s.sources?.size)?null:s.lastOutputEndAt??null;
          const timing={generation:message.generation,first_audio_sequence:message.audio_sequence,
            first_packet_at_ms:arrival,first_packet_context_state:s.ctx.state,first_packet_context_s:s.ctx.currentTime,
            previous_output_end_at_ms:previousEnd,gap_since_previous_end_ms:previousEnd===null?null:arrival-previousEnd,
            playback_to_first_packet_ms:s.playbackNotice?.generation===message.generation?arrival-s.playbackNotice.at:null,
            scheduled_start_context_s:null,start_observed_at_ms:null};
          s.playbackNotice=null;
          (s.playbackTimings||(s.playbackTimings=[])).push(timing);if(s.playbackTimings.length>60)s.playbackTimings.shift();
          s.receivingPart={generation:message.generation,timing,duration:0,complete:false};
        }
        message.playbackPart=s.receivingPart;message.playbackTiming=s.receivingPart.timing;
        try{s.receivingPart.duration+=atob(message.pcm).length/64000;}catch{}
      }else{
        completePart(s,s.receivingPart,message);s.receivingPart=null;
        if(message.type==='audio_end')s.captionPrefixes?.delete(message.generation);
      }
      s.busy=true;s.generations.add(message.generation);s.lastPacket=Date.now();s.gate.finish(true);s.queueBytes+=message.pcm?.length||0;
      if(s.queueBytes>16*1024*1024){void finish(s,'声音积压过多，请重新拨号');return;}
      s.queue.push(message);void send(s,{type:'audio_ack',audio_sequence:message.audio_sequence}).catch(()=>{});void drain(s);return;
    }
    if(message.type==='line'||message.type==='transcript'){
      const line=message.line||message,assistant=['assistant'].includes(line.role);
      if(assistant&&['failed','interrupted'].includes(line.status))discardIncompletePart(s);
      void saveLine(s,assistant?'AI':'我',line.text,line.id||message.id);return;
    }
    if(message.type==='call'&&['ended','failed','cancelled','missed'].includes(message.state)){void finish(s,'通话已结束',false);return;}
    if(message.type==='call'&&message.state==='recovering'){recovery(s,'upstream',true);return;}
    if(message.type==='call'&&message.state==='connected'){recovery(s,'upstream',false);return;}
    if(message.type==='error'){if(message.code==='reconnect_failed')void finish(s,message.message,false);else status(message.message||'这句话未能送出');}
  }
  async function poll(s){
    let cursor=0,failedAt=null;
    while(active(s))try{
      const remaining=failedAt===null?25000:Math.max(1,95000-(performance.now()-failedAt));
      const data=await json(`/api/calls/${s.id}/events?after=${cursor}`,undefined,Math.min(25000,remaining),s.abort.signal);
      if(!active(s))break;failedAt=null;recovery(s,'poll',false);
      for(const item of data.events)handle(s,item.data);cursor=data.cursor;if(data.closed){await finish(s,'通话已结束',false);break;}
    }catch(error){
      if(!active(s))break;if(failedAt===null)failedAt=performance.now();
      if(!retryable(error)||performance.now()-failedAt>=95000){await finish(s,'连接中断太久，未能重连，请重新拨号');break;}
      recovery(s,'poll',true);await delay(s,Math.min(1000,95000-(performance.now()-failedAt)));
    }
  }
  async function sendText(text){
    const s=current;if(!s?.ready)throw Error('电话还没接通');wake(s);const id=crypto.randomUUID();
    await send(s,{type:'draft',id,text});await send(s,{type:'submit',id,text,manual:true});
    await saveLine(s,'我',text,id);
    try{return {sent:true,history:await json(FACE+'/api/messages')};}catch{return {sent:true,history:null};}
  }
  function beginHolding(){const s=current;if(!s?.ready||s.busy||s.recovering)return;s.gate.finish(true);s.holding=true;wake(s);status('正在听 · 松开发送');}
  function endHolding(cancel=false){const s=current;if(!s?.holding)return;s.holding=false;s.gate.finish(cancel);status(s.manual?'按住脸说话':'已接通 · 自动听');}
  async function reject(){const id=invitation?.id;invitation=null;controls();if(id)try{await json(`/api/calls/${id}/end`,{});}catch{status('本机已拒接，远端待确认');}}
  async function checkIncoming(){
    clearTimeout(incomingTimer);
    try{const data=await json('/api/incoming',undefined,3000);if(!current){invitation=data.incoming;if(off&&invitation){await reject();return;}controls();if(invitation)status('AI来电 · 上划接听，下划拒接');}}
    catch{ /* The face remains usable while the call service reconnects. */ }
    incomingTimer=setTimeout(checkIncoming,2000);
  }
  let swipe=null,holdTimer;
  const surface=$('glass');
  surface.addEventListener('pointerdown',event=>{
    if(current||invitation){event.preventDefault();event.stopImmediatePropagation();}
    if(!event.isPrimary){clearTimeout(holdTimer);swipe=null;endHolding(true);return;}
    if(document.body.classList.contains('touch-locked'))return;
    swipe={id:event.pointerId,x:event.clientX,y:event.clientY,at:Date.now()};
    if(current||invitation){event.preventDefault();event.stopImmediatePropagation();surface.setPointerCapture(event.pointerId);if(current?.ready)holdTimer=setTimeout(beginHolding,500);}
  },true);
  surface.addEventListener('pointermove',event=>{
    if(!swipe||event.pointerId!==swipe.id)return;
    if(Math.hypot(event.clientX-swipe.x,event.clientY-swipe.y)>15)clearTimeout(holdTimer);
    if(current||invitation){event.preventDefault();event.stopImmediatePropagation();}
  },true);
  surface.addEventListener('pointerup',event=>{
    clearTimeout(holdTimer);if(!swipe||event.pointerId!==swipe.id)return;
    const dy=event.clientY-swipe.y,dx=Math.abs(event.clientX-swipe.x),duration=Date.now()-swipe.at;swipe=null;
    const s=current,wasHolding=s?.holding;endHolding();
    if(s||invitation){event.preventDefault();event.stopImmediatePropagation();}
    if(wasHolding||duration>1800||dx>Math.abs(dy)*.6)return;
    if(invitation){if(dy<-70)void dial(invitation.id);else if(dy>70)void reject();}
    else if(s){if(dy>(s.ready?innerHeight/2:70))void finish(s,s.ready?'已挂断':'已取消');}
    else if(dy<-80)void dial();
  },true);
  surface.addEventListener('pointercancel',()=>{clearTimeout(holdTimer);swipe=null;endHolding(true);},true);
  $('call-link').addEventListener('click',event=>{event.preventDefault();if(invitation)void dial(invitation.id);else if(!current)void dial();});
  $('call-answer').addEventListener('click',()=>{if(invitation)void dial(invitation.id);});
  $('call-end').addEventListener('click',()=>current?void finish(current):void reject());
  $('call-ptt-mode').addEventListener('click',()=>{if(current){current.gate.finish(true);current.manual=!current.manual;controls();status(current.manual?'按住脸半秒后说话，松开发送':'已接通 · 自动听');}});
  $('call-mute').addEventListener('click',()=>$('mute-toggle').click());
  $('call-resume').addEventListener('click',()=>{if(current)void current.ctx.resume().then(controls);});
  window.addEventListener('face-settings',event=>{
    muted=event.detail.muted!==false;off=event.detail.off_mode===true;
    if(current?.gain)current.gain.gain.value=muted?0:1;
    if(event.detail.phone_call_active&&current)void finish(current,'手机来电，语音已结束');else if(off&&current)void finish(current,'已全关，电话结束');else controls();
  });
  document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible'&&current?.ctx?.state==='suspended')void current.ctx.resume().catch(()=>{});});
  window.addEventListener('pagehide',()=>{clearTimeout(incomingTimer);if(current){navigator.sendBeacon(BASE+`/api/calls/${current.id}/end`);void finish(current,'已离开电话',false);}});
  if('mediaSession' in navigator){
    try{navigator.mediaSession.setActionHandler('pause',()=>{if(current)void current.ctx.suspend();});navigator.mediaSession.setActionHandler('play',()=>{if(current)void current.ctx.resume();});}catch{}
  }
  window.faceCall={dial,sendText,get audioSource(){return current?.sources?.size?current.gain:null;},get activityAt(){return current?.lastSound||0;},get active(){return !!current;},get busy(){return !!current||!!invitation;},get speaking(){return !!current?.playing&&current.ctx.state==='running'&&!muted;},
    diagnostic:(includePlayback=false)=>({state:current?(current.ready?'connected':'dialing'):invitation?'ringing':'idle',sleeping:current?.sleeping===true,automatic:current?!current.manual:true,mic_pinned:current?.micPinned===true,output_selected:current?.outputSelected===true,
      ...(includePlayback?{playback:(current?.playbackTimings||[]).map(row=>({...row}))}:{})})};
  controls();checkIncoming();
})();

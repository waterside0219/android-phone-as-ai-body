const assert=require('node:assert/strict');
const {client,audio,part,end,approx}=require('./test-playback-start.cjs');
const cases={
  async 'uncertain microphone upload discards remaining packets without ending the call'(){
    for(const failing of ['utterance_start','utterance_audio']){
      const c=await client();try{
        let failed=false;
        c.model.transport=async message=>{if(message.type===failing&&!failed){failed=true;throw Error('lost upload');}};
        c.capture(new Float32Array(48000).fill(.2));await c.flush();await c.advance(1000);await c.flush();
        const failedAt=c.model.sent.findIndex(x=>x.type===failing);
        assert.ok(failedAt>=0);
        const later=c.model.sent.slice(failedAt+1);
        assert.ok(!later.some(x=>x.type==='utterance_audio'),'no residual packets after uncertain '+failing);
        assert.ok(later.some(x=>x.type==='utterance_end'&&x.discard===true),'discard uncertain capture');
        assert.equal(c.api.active,true);
      }finally{await c.hangup();}
    }
  },
  async 'buffered audio is acknowledged before the segment end can arrive'(){
    const c=await client();try{
      const packet=audio(1,256);await c.deliver(packet);
      assert.equal(c.model.sources.length,0,'segment is still buffering');
      assert.ok(c.model.sent.some(x=>x.type==='audio_ack'&&x.audio_sequence===packet.audio_sequence),'release gateway buffer on receipt');
    }finally{await c.hangup();}
  },
  async 'failed partial synthesis does not block the next reply'(){
    const c=await client();try{
      await c.deliver({type:'playback',generation:1},audio(1,256));await c.advance(1100);
      await c.deliver({type:'line',line:{id:'ai-1',role:'assistant',text:'没合成完的半句。',status:'failed'}},
        {type:'error',message:'这次声音合成失败'});
      assert.ok(!c.model.classes.has('call-attention'),'failed segment is no longer buffering');
      await c.deliver({type:'playback',generation:2},audio(2,1000),{...part(2),text:'这是后来的完整回复。'},end(2));
      await c.advance(500);assert.equal(c.model.sources.length,1);assert.equal(c.model.captions.at(-1).text,'这是后来的完整回复。');
    }finally{await c.hangup();}
  },
  async 'late source-ended callback cannot resurrect a hung-up caption'(){
    const c=await client();try{
      await c.deliver(audio(1,4000),{...part(1),text:'挂断以后不应该留在这里。'},end(1));await c.advance(500);
      assert.ok(c.model.captions.at(-1).text);await c.hangup();await c.advance(200);
      assert.equal(c.model.captions.at(-1).text,'');
    }finally{await c.hangup();}
  },
  async 'interrupted or superseded partial reply cannot block newer speech or cut complete audio'(){
    for(const terminalLine of [true,false]){
      const c=await client();try{
        await c.deliver({type:'playback',generation:1},audio(1,1000),{...part(1),text:'完整的前半句。'},audio(1,256));
        await c.advance(300);await c.api.sendText('改问下一件事');
        if(terminalLine)await c.deliver({type:'interrupt',generation:1},
          {type:'line',line:{id:'ai-1',role:'assistant',text:'未合成完的后半句。',status:'interrupted'}});
        await c.deliver({type:'playback',generation:2},audio(2,1000),{...part(2),text:'接着说新的回复。'},end(2));
        await c.advance(2000);
        assert.equal(c.model.sources.length,2,'only complete segments play, including the new reply');
        assert.ok(!c.model.sources[0].stopped,'complete earlier audio stays intact');
        approx(c.model.sources[1].at,c.model.sources[0].at+1,'complete parts stay contiguous');
        assert.ok(c.model.captions.some(x=>x.text==='接着说新的回复。'));
      }finally{await c.hangup();}
    }
  },
  async 'slow acknowledgement does not stall already received audio'(){
    const c=await client();let release;try{
      c.model.ackWait=new Promise(resolve=>release=resolve);
      await c.deliver(audio(1,256),audio(1,256),audio(1,256),end(1));await c.advance(800);
      assert.equal(c.model.sources.length,3,'all available packets schedule before ACK returns');
      for(let i=1;i<3;i++)approx(c.model.sources[i].at,c.model.sources[i-1].at+.256,'no ACK-induced gap');
    }finally{release();await c.flush();await c.hangup();}
  },
  async 'incomplete sentence buffers and does not drip one packet at a time'(){
    const c=await client();try{
      await c.deliver(audio(1,256));await c.advance(1100);
      assert.equal(c.model.sources.length,0,'wait for the complete PCM segment');
      assert.match(c.status(),/缓冲/);assert.ok(c.model.classes.has('call-attention'));
      await c.deliver(audio(1,256));await c.advance(700);assert.equal(c.model.sources.length,0);
      await c.deliver({...part(1),text:'先说这一句。然后说下一句。',line_id:'ai-1'},end(1));await c.advance(500);
      assert.equal(c.model.sources.length,2);approx(c.model.sources[1].at,c.model.sources[0].at+.256,'buffered PCM stays gapless');
      assert.equal(c.model.sources[0].buffer.duration,.256,'normal rate, no time stretch');
    }finally{await c.hangup();}
  },
  async 'captions follow PCM clock, do not show future text, and survive suspension'(){
    const c=await client();try{
      await c.deliver({type:'line',line:{id:'ai-1',role:'assistant',text:'第一句。第二句。'}},audio(1,4000),
        {...part(1),text:'第一句。第二句。',line_id:'ai-1'},end(1));
      assert.ok(c.model.captions.every(x=>!x.text),'receiving text must not display it before audio');
      await c.advance(500);assert.equal(c.model.captions.at(-1)?.text,'第一句。');
      await c.ctx.suspend();await c.advance(6000);assert.equal(c.model.captions.at(-1).text,'第一句。','pause freezes captions');
      await c.ctx.resume();await c.advance(1900);assert.equal(c.model.captions.at(-1).text,'第二句。');
      assert.ok(!c.model.sent.some(x=>x.type==='played'),'not played until the physical rendering timeline ends');
      await c.advance(1800);assert.equal(c.model.captions.at(-1).text,'');
      assert.ok(c.model.sent.some(x=>x.type==='played'));
    }finally{await c.hangup();}
  },
  async 'cumulative part text is displayed only once at its matching audio'(){
    const c=await client();try{
      await c.deliver(audio(1,1000),{...part(1),text:'第一句。',line_id:'ai-1'},audio(1,1000),
        {...part(1),part:2,text:'第一句。\n我等你。',line_id:'ai-1'},end(1));
      await c.advance(500);assert.equal(c.model.captions.at(-1).text,'第一句。');
      await c.advance(1000);assert.equal(c.model.captions.at(-1).text,'我等你。');
      assert.ok(!c.model.captions.some(x=>x.text.includes('\n')),'no accumulated whole paragraph');
    }finally{await c.hangup();}
  },
  async 'poll transport recovers with the same cursor and a visible warning'(){
    const c=await client();try{
      await c.deliver({type:'line',line:{id:'u-1',role:'you',text:'测试'}});const last=c.model.polls.at(-1);
      await c.rejectPoll();assert.equal(c.api.active,true);assert.match(c.status(),/重连/);assert.ok(c.model.classes.has('call-attention'));
      await c.advance(1000);assert.equal(c.model.polls.at(-1),last,'poll retry keeps cursor and call ID');
      await c.deliver({type:'call',state:'connected'});assert.equal(c.api.active,true);assert.ok(!c.model.classes.has('call-attention'));
    }finally{await c.hangup();}
  },
  async 'recovering state blocks capture, stays visible, and terminal user hangup never retries'(){
    const c=await client();try{
      await c.deliver({type:'call',state:'recovering'});assert.match(c.status(),/重连/);assert.ok(c.model.classes.has('call-attention'));
      const polls=c.model.polls.length;await c.hangup();await c.advance(31000);assert.equal(c.model.polls.length,polls);
      assert.ok(!c.model.classes.has('call-attention'));
    }finally{await c.hangup();}
  },
  async 'slow ACKs coalesce without overflowing the upload queue'(){
    const c=await client();let release;try{
      c.model.ackWait=new Promise(resolve=>release=resolve);
      await c.deliver(...Array.from({length:100},()=>audio(1,256)),end(1));await c.advance(20000);
      assert.equal(c.api.active,true,'already buffered audio must not end a call because ACKs are delayed');
      assert.ok(c.model.sources.length>60);
    }finally{release();await c.flush();await c.hangup();}
  }
};
(async()=>{let failed=0;for(const [name,run]of Object.entries(cases))try{await run();console.log('PASS',name);}catch(e){failed++;console.error('FAIL',name,e.stack);}
console.log(`${Object.keys(cases).length-failed}/${Object.keys(cases).length} recovery/caption checks passed`);if(failed)process.exitCode=1;})().catch(e=>{console.error(e);process.exitCode=1;});

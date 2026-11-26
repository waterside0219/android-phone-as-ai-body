// Real Chromium AudioContext, local silent MediaStream, fake call transport.
// Shared mute remains on: this checks rendering clocks, not physical speaker output.
const {chromium}=require(process.env.PLAYWRIGHT_PATH||'playwright');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
(async()=>{
  const browser=await chromium.launch({headless:true});
  try{
    const page=await browser.newPage(),errors=[];
    page.on('pageerror',e=>errors.push(String(e)));
    await page.route('**/*',route=>route.fulfill({contentType:'text/html',body:'<html></html>'}));
    await page.goto('http://localhost/');
    await page.setContent(fs.readFileSync(path.join(__dirname,'../face/index.html'),'utf8').replace(/<script[\s\S]*?<\/script>/g,''));
    await page.evaluate(()=>{
      const NativeContext=window.AudioContext;
      window.faceLive2D={setAudioSource(source){window.lipSource=source;}};
      window.probe={next:[{type:'ready'}],sources:[],polls:0};
      window.AudioContext=class extends NativeContext{
        constructor(...args){super(...args);probe.ctx=this;}
        createBufferSource(){const source=super.createBufferSource(),start=source.start.bind(source);
          source.start=(at=0)=>{probe.sources.push({at,context:this.currentTime,state:this.state,duration:source.buffer.duration,
            samples:source.buffer.length,first:source.buffer.getChannelData(0)[0]});start(at);};return source;}
      };
      Object.defineProperty(navigator,'mediaDevices',{value:{getUserMedia:async()=>{
        probe.mic=probe.ctx.createMediaStreamDestination();return probe.mic.stream;
      },enumerateDevices:async()=>[]}});
      window.fetch=async(url,options={})=>{
        if(url.includes('/events?')){
          await new Promise(resolve=>setTimeout(resolve,10));probe.polls++;
          return {ok:true,json:async()=>({events:probe.next.splice(0).map(data=>({data})),cursor:probe.polls})};
        }
        return {ok:true,json:async()=>url.endsWith('/api/incoming')?{incoming:null}:{ok:true,remote_confirmed:true}};
      };
      window.packet=(generation,sequence)=>{const samples=new Int16Array(16384);samples[0]=12345;
        return {type:'audio',generation,audio_sequence:sequence,pcm:btoa(String.fromCharCode(...new Uint8Array(samples.buffer)))};};
    });
    await page.addScriptTag({path:process.env.CALL_APP_PATH||path.join(__dirname,'app.js')});
    await page.evaluate(()=>faceCall.dial());await page.waitForFunction(()=>faceCall.diagnostic().state==='connected');
    await page.evaluate(()=>probe.next.push(packet(1,1),packet(1,2),{type:'audio_end',generation:1,audio_sequence:3}));
    await page.waitForFunction(()=>probe.sources.length===2);await page.waitForFunction(()=>faceCall.diagnostic(true).playback?.[0].start_observed_at_ms);
    const lip=await page.evaluate(()=>typeof lipSource==='object' && lipSource?.constructor.name==='GainNode');assert.equal(lip,true,'actual output gain must feed lip sync');
    const scheduled=await page.evaluate(()=>probe.sources);
    assert.ok(scheduled[0].at-scheduled[0].context>=.14,'native first source has wake-up lead');
    assert.ok(Math.abs(scheduled[1].at-scheduled[0].at-scheduled[0].duration)<1e-9,'native buffers are contiguous');
    assert.equal(scheduled[0].samples,16384);assert.equal(scheduled[0].first,12345/32768);
    await page.waitForFunction(()=>probe.ctx.currentTime>probe.sources[1].at+probe.sources[1].duration+.05);
    await page.evaluate(async()=>{await probe.ctx.suspend();probe.next.push(packet(2,4),{type:'audio_end',generation:2,audio_sequence:5});});
    await page.waitForFunction(()=>probe.sources.length===3);await page.waitForFunction(()=>faceCall.diagnostic(true).playback?.[1].start_observed_at_ms);
    const result=await page.evaluate(()=>({sources:probe.sources,timings:faceCall.diagnostic(true).playback}));
    assert.equal(result.sources[2].state,'running');assert.equal(result.timings[1].first_packet_context_state,'suspended');
    assert.ok(result.sources[2].at-result.sources[2].context>=.14);
    await page.locator('#call-end').click();await page.waitForFunction(()=>faceCall.diagnostic().state==='idle'&&probe.ctx.state==='closed');
    assert.equal(await page.evaluate(()=>lipSource),null,'hangup clears lip source');
    assert.deepEqual(errors,[]);
    console.log('PASS native Chromium audio rendering: complete first samples, 150ms lead, gapless buffers, suspended resume, cleanup (not phone/speaker acceptance)');
    console.log(JSON.stringify(result,null,2));
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});

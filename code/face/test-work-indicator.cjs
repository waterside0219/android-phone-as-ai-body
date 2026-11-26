const {chromium}=require(process.env.PLAYWRIGHT_PATH||'playwright');
const fs=require('node:fs'),http=require('node:http'),path=require('node:path'),assert=require('node:assert/strict');
const output=path.resolve(process.env.EVIDENCE_DIR||'.private/j-preview');
fs.mkdirSync(output,{recursive:true});
const server=http.createServer((req,res)=>{
  const file=path.join(__dirname,req.url.split('?')[0]==='/'?'index.html':req.url.split('?')[0]);
  fs.readFile(file,(error,data)=>{if(error)return res.writeHead(404).end();res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript':file.endsWith('.html')?'text/html':'application/octet-stream');res.end(data);});
});
const defaults={state:'在等',camera_mode:'off',camera_active:false,presence:false,muted:true,off_mode:false};
async function routes(page,state){
  await page.route('http://127.0.0.1:8192/**',r=>r.fulfill({json:r.request().url().includes('/api/messages')?{messages:[],unread:0}:state,headers:{'Access-Control-Allow-Origin':'*'}}));
  await page.route('http://127.0.0.1:8191/**',r=>r.fulfill({body:'',contentType:'text/javascript'}));
}
(async()=>{
  await new Promise(r=>server.listen(0,'127.0.0.1',r));
  const browser=await chromium.launch({headless:true}),failures=[],report={checks:[],local_simulation:true};
  const check=(label,fn)=>{try{fn();report.checks.push({label,passed:true});}catch(error){failures.push(error);report.checks.push({label,passed:false,message:error.message});}};
  const url='http://127.0.0.1:'+server.address().port;
  try{
    const page=await browser.newPage({viewport:{width:311,height:535}}),state={...defaults},errors=[];
    page.on('pageerror',e=>errors.push(e.message));await routes(page,state);
    await page.clock.install({time:new Date('2026-10-29T12:00:00Z')});await page.goto(url);await page.clock.pauseAt(new Date('2026-10-29T12:00:01Z'));
    const idle=await page.evaluate(()=>{
      const result=[];
      for(const present of [false,true]){
        const names=new Set();let minBlink=1,maxBlink=0;
        const start=Date.now();
        for(let elapsed=0;elapsed<=300000;elapsed+=50){
          eyeExpressions.update({state:'waiting',present,now:start+elapsed,blink:elapsed%4000<150});
          const d=eyeExpressions.diagnostic();names.add(d.name);minBlink=Math.min(minBlink,d.blink);maxBlink=Math.max(maxBlink,d.blink);
        }
        result.push({present,duration_ms:300000,names:[...names],minBlink,maxBlink});
      }
      return result;
    });report.idle=idle;
    for(const item of idle){check('idle 5 minutes normal only; present='+item.present,()=>assert.deepEqual(item.names,['normal']));check('blink still closes and opens; present='+item.present,()=>assert.ok(item.minBlink<.1&&item.maxBlink>.99));}
    // Reload resets the expression time after the five-minute component sweep.
    await page.reload();await page.clock.runFor(100);
    const loaded=await page.evaluate(()=>!!window.workIndicator);
    check('real face entry loads work-indicator',()=>assert.ok(loaded));
    if(loaded){
      async function deliver(next){Object.assign(state,{...defaults,...next});const start=await page.evaluate(()=>Date.now());await page.evaluate(()=>pollFace());await page.clock.runFor(50);return {elapsed_ms:(await page.evaluate(()=>Date.now()))-start,...await page.evaluate(()=>workIndicator.diagnostic())};}
      report.working=await deliver({state:'在干活'});
      check('Chinese working state shows typing within 2s',()=>{assert.equal(report.working.mode,'typing');assert.ok(report.working.elapsed_ms<=2000);});
      const exclusion=await page.evaluate(()=>({face:faceExclusion(),blocks:document.querySelector('.work-block').parentElement.getBoundingClientRect().bottom}));check('subtitles exclude the typing row',()=>assert.ok(exclusion.face.bottom>=exclusion.blocks));
      const typing=await page.evaluate(()=>{
        const keys=[...document.querySelectorAll('.work-key')],snapshots=[];
        for(let t=0;t<6000;t+=50){workIndicator.update({state:'working',now:Date.now()+t});snapshots.push(keys.map(n=>({alpha:Number(n.getAttribute('opacity')),press:n.transform.baseVal.consolidate()?.matrix.f||0})));}
        const box=keys[0]?.parentElement.getBoundingClientRect();
        return {hands:document.querySelectorAll('.work-hand').length,rows:[...new Set(keys.map(k=>k.dataset.row))],snapshots,top:box?.top,width:box?.width,height:innerHeight,screenWidth:innerWidth,letters:keys.map(k=>k.dataset.key).join(''),spaceRatio:(keys.find(k=>k.dataset.key===' ')?.getBBox().width||0)/keys[0].getBBox().width,positions:Object.fromEntries(keys.map(k=>[k.dataset.key,Number(k.dataset.x)])),geometry:Object.fromEntries(keys.map(k=>{const b=k.getBBox();return [k.dataset.key,{x:b.x,y:b.y,width:b.width,height:b.height}];})),faces:keys.every(k=>k.querySelectorAll('path').length===2)};
      });
      check('invisible QWERTY keyboard has staggered rows and pressed keycaps, no hands',()=>{
        assert.equal(typing.hands,0);assert.equal(typing.rows.length,4);assert.equal(typing.letters,'qwertyuiopasdfghjklzxcvbnm ');assert.ok(typing.faces);
        assert.ok(typing.positions.a<typing.positions.q&&typing.positions.z<typing.positions.a);
        for(const frame of typing.snapshots){const lit=frame.filter(k=>k.alpha>0);assert.ok(lit.filter(k=>k.alpha>.3).length<=5);assert.ok(lit.length<=12);assert.ok(lit.every(k=>k.alpha<=.5));}
        const touched=Array.from({length:26},(_,i)=>typing.snapshots.some(frame=>frame[i].alpha>0)?i:-1).filter(i=>i>=0);assert.ok(touched.length>=14);
        assert.ok(typing.snapshots.some(frame=>frame.every(k=>k.alpha===0)),'natural phrase pause');
        assert.ok(typing.snapshots.some(frame=>frame.some(k=>k.press>=1.9)),'visible key downstroke');
      });
      check('viewer faces the typist: spacebar is nearest the face, letter rows widen toward the viewer and keys are reversed left to right',()=>{
        const g=typing.geometry;
        assert.ok(g[' '].y<g.z.y&&g.z.y<g.a.y&&g.a.y<g.q.y,'space / ZX / AS / QW from face to viewer');
        assert.ok(g.q.x>g.p.x&&g.z.x>g.m.x,'typist left appears on viewer right');
        assert.ok(g.q.width>g.a.width&&g.a.width>g.z.width,'nearer letter keys appear larger');
      });
      check('word spaces press a wide spacebar, and struck keys rebound before their short afterglow fades',()=>{
        assert.ok(typing.spaceRatio>=4,'recognizable long spacebar');
        assert.ok(typing.snapshots.some(frame=>frame[26]?.alpha>.2),'spacebar is actually struck');
        assert.ok(typing.snapshots.some(frame=>frame.some(k=>k.press>=5)),'clear downstroke');
        assert.ok(typing.snapshots.some(frame=>frame.some(k=>k.press<-.3)),'small rebound past rest');
        assert.ok(typing.snapshots.some(frame=>frame.filter(k=>k.alpha>0&&k.alpha<=.3&&Math.abs(k.press)<.01).length>=3),'several struck keys remain faintly visible at rest');
        for(let key=0;key<typing.letters.length;key++){
          let hit=false;for(const frame of typing.snapshots){if(frame[key]?.press>0)hit=true;if(!hit)assert.equal(frame[key]?.alpha,0,'unstruck keys stay invisible');}
        }
      });
      check('keyboard reads as a broad desk plane well below the face',()=>{assert.ok(typing.top>typing.height*.70);assert.ok(typing.width>typing.screenWidth*.50);});
      report.keyboard={top:typing.top,width:typing.width,viewport:[typing.screenWidth,typing.height],letters:typing.letters};
      const coordinated=await page.evaluate(()=>{
        const start=Date.now()+10000,samples=[];
        workIndicator.update({state:'waiting',now:start-1000});eyeExpressions.update({state:'waiting',now:start-1000});eyeExpressions.update({state:'waiting',now:start-50});
        for(let t=0;t<7000;t+=50){
          workIndicator.update({state:'working',now:start+t});eyeExpressions.update({state:'working',now:start+t});
          const matrix=document.querySelector('#glasses-rig').transform.baseVal.consolidate().matrix;
          const gaze=document.querySelector('.eye-gaze').transform.baseVal.consolidate().matrix;
          samples.push({t,headY:matrix.f,gazeY:gaze.f,lit:[...document.querySelectorAll('.work-key')].filter(k=>Number(k.getAttribute('opacity'))>0).length});
        }
        const keyboardSeparate=!document.querySelector('.work-key').closest('#glasses-rig');
        workIndicator.update({state:'waiting',now:start+7000});
        for(let t=7000;t<=8500;t+=50)eyeExpressions.update({state:'waiting',now:start+t});
        const stopped=eyeExpressions.diagnostic().work_motion;
        return {samples,keyboardSeparate,stopped};
      });report.coordinated=coordinated;
      check('eyes and head prepare before typing and move through the same phrase',()=>{
        const first=coordinated.samples.find(s=>s.lit>0);assert.ok(first.t>=450&&first.t<2000);
        const during=coordinated.samples.filter(s=>s.t>=800&&s.t<6000);
        assert.ok(Math.max(...during.map(s=>s.headY))-Math.min(...during.map(s=>s.headY))>3);
        assert.ok(Math.max(...during.map(s=>s.gazeY))-Math.min(...during.map(s=>s.gazeY))>1);
        assert.equal(coordinated.keyboardSeparate,true);assert.ok(coordinated.stopped.every(v=>Math.abs(v)<.1));
      });
      report.thinking=await deliver({state:'在想'});
      check('thinking has no new work overlay or scroll nodes',()=>{assert.equal(report.thinking.mode,null);});
      const scrollNodes=await page.locator('.work-lines').count();check('deferred scroll effect is absent',()=>assert.equal(scrollNodes,0));
      report.drift=await page.evaluate(()=>{
        const samples=[];for(const elapsed of [0,30000,60000,90000]){workIndicator.update({state:'working',now:Date.now()+elapsed});samples.push({elapsed,...workIndicator.diagnostic()});}return samples;
      });
      check('typing drifts every 30s by at most 3 SVG units per axis',()=>{
        assert.equal(new Set(report.drift.map(s=>s.drift.join(','))).size,4);
        for(const s of report.drift){assert.ok(s.drift.every(n=>Math.abs(n)<=3));assert.ok(s.max_opacity<=.55);}
      });
      report.waiting=await deliver({state:'在等'});
      check('waiting clears within 3s',()=>{assert.equal(report.waiting.mode,null);assert.ok(report.waiting.elapsed_ms<=3000);});
      report.stuck=await deliver({state:'卡住了'});
      check('stuck has no work overlay and retains original expression',()=>assert.equal(report.stuck.mode,null));
      const stuckSequence=await page.evaluate(()=>eyeExpressions.sequences.stuck);check('stuck sequence unchanged',()=>assert.deepEqual(stuckSequence,['stuck','angry']));
      const suppressed=await page.evaluate(()=>{
        const result=[];for(const flags of [{closed:true},{quiet:true},{cooling:true},{hidden:true}]){workIndicator.update({state:'working',now:Date.now()});workIndicator.update({state:'working',now:Date.now()+1,...flags});result.push(workIndicator.diagnostic().mode);}return result;
      });check('closed/off/cooling/hidden suppress indicators immediately',()=>assert.deepEqual(suppressed,[null,null,null,null]));
      await deliver({state:'在干活'});await deliver({state:'在干活',thermal_hold:true});
      const coolingMode=await page.evaluate(()=>workIndicator.diagnostic().mode);check('cooling hides overlay',()=>assert.equal(coolingMode,null));
      await deliver({state:'在等',thermal_hold:false});
    }
    check('no browser exceptions',()=>assert.deepEqual(errors,[]));
    fs.writeFileSync(path.join(output,'behavior.json'),JSON.stringify(report,null,2)+'\n');
    await page.close();
    for(const item of report.checks)console.log((item.passed?'PASS ':'FAIL ')+item.label+(item.message?' — '+item.message:''));
    if(failures.length)throw new AggregateError(failures,'J behavior checks failed');
    if(process.env.LIVE_LATENCY){
      const live=await browser.newPage({viewport:{width:311,height:535}}),wire={...defaults};await routes(live,wire);await live.goto(url);await live.waitForFunction(()=>!!window.workIndicator);
      const samples=[];
      for(const [state,expected,limit] of [['在干活','typing',2000],['在想',null,3000],['在干活','typing',2000],['在等',null,3000]]){
        const start=Date.now();wire.state=state;await live.waitForFunction(expected=>workIndicator.diagnostic().mode===expected&&(expected!=='typing'||[...document.querySelectorAll('.work-key')].some(k=>Number(k.getAttribute('opacity'))>0)),expected,{timeout:limit});
        const elapsed_ms=Date.now()-start;samples.push({state,expected,elapsed_ms,limit_ms:limit});assert.ok(elapsed_ms<=limit);
      }
      fs.writeFileSync(path.join(output,'poll-latency-local.json'),JSON.stringify({scope:'Mac real-clock /api/face polling, simulated relay response; no VPS/phone',samples},null,2)+'\n');await live.close();console.log('PASS actual local poll loop latency',JSON.stringify(samples));
    }
    if(process.env.IDLE_WALL){
      const live=await browser.newPage({viewport:{width:311,height:535}});await routes(live,{...defaults,presence:true});
      await live.goto(url);await live.waitForFunction(()=>!!window.workIndicator);
      await live.evaluate(()=>{
        window.idleProof={started:Date.now(),samples:0,names:[],blink_closures:0,min_blink:1,max_blink:0,overlays:0};let wasClosed=false;
        window.idleProofTimer=setInterval(()=>{const d=eyeExpressions.diagnostic(),p=idleProof;p.samples++;if(!p.names.includes(d.name))p.names.push(d.name);p.min_blink=Math.min(p.min_blink,d.blink);p.max_blink=Math.max(p.max_blink,d.blink);if(d.blink<.2&&!wasClosed)p.blink_closures++;wasClosed=d.blink<.2;if(workIndicator.diagnostic().mode)p.overlays++;},50);
      });
      console.log('WALL idle observation started (300 seconds, real browser clock)');
      await live.waitForTimeout(300100);
      const proof=await live.evaluate(()=>{clearInterval(idleProofTimer);return {...idleProof,elapsed_ms:Date.now()-idleProof.started};});
      fs.writeFileSync(path.join(output,'idle-wall-5min.json'),JSON.stringify(proof,null,2)+'\n');
      assert.ok(proof.elapsed_ms>=300000);assert.deepEqual(proof.names,['normal']);assert.equal(proof.overlays,0);assert.ok(proof.blink_closures>=20&&proof.min_blink<.2&&proof.max_blink>.99);
      await live.close();console.log('PASS real-clock idle 5 minutes, normal eyes and natural blinking only');
    }
    if(process.env.RECORD_PREVIEWS){
      for(const [name,faceState] of [['typing','在干活']].filter(([name])=>process.env.RECORD_PREVIEWS==='1'||process.env.RECORD_PREVIEWS===name)){
        const context=await browser.newContext({viewport:{width:810,height:1392},recordVideo:{dir:output,size:{width:810,height:1392}}});
        const preview=await context.newPage(),state={...defaults};await routes(preview,state);
        await preview.goto(url);await preview.waitForTimeout(1000);state.state=faceState;
        await preview.waitForFunction(()=>workIndicator.diagnostic().mode!==null);
        await preview.waitForTimeout(3000);await preview.screenshot({path:path.join(output,name+'.png')});
        await preview.waitForTimeout(11000);state.state='在等';await preview.waitForTimeout(3000);
        const video=preview.video();await context.close();await video.saveAs(path.join(output,name+'.webm'));fs.unlinkSync(await video.path());
        console.log('RECORDED '+name+' 14s working plus entry/exit');
      }
    }
  }finally{await browser.close();server.close();}
})().catch(error=>{console.error(error);server.close();process.exitCode=1;});

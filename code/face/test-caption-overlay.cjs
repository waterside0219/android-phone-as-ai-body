// K: actual face/J/layout code, local Chromium; no camera or provider requests.
const {chromium}=require(process.env.PLAYWRIGHT_PATH||'playwright');
const fs=require('node:fs'),http=require('node:http'),path=require('node:path'),assert=require('node:assert/strict');
const output=process.env.EVIDENCE_DIR; if(output)fs.mkdirSync(output,{recursive:true});
const server=http.createServer((req,res)=>{
  const name=req.url.split('?')[0]==='/'?'index.html':req.url.split('?')[0];
  const baseline=process.env.BASELINE_DIR&&path.join(process.env.BASELINE_DIR,name);
  const file=baseline&&fs.existsSync(baseline)?baseline:path.join(__dirname,name);
  fs.readFile(file,(error,data)=>{if(error)return res.writeHead(404).end();res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript':file.endsWith('.html')?'text/html':'application/octet-stream');res.end(data);});
});
(async()=>{await new Promise(r=>server.listen(0,'127.0.0.1',r));const browser=await chromium.launch({headless:true});
  const report=[],failures=[];
  try{
    const page=await browser.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
    const state={state:'在等',camera_mode:'off',camera_enabled:false,camera_allowed:false,muted:true,off_mode:false};
    await page.route('http://127.0.0.1:8192/**',r=>r.fulfill({json:state,headers:{'Access-Control-Allow-Origin':'*'}}));
    await page.route('http://127.0.0.1:8191/**',r=>r.fulfill({body:'',contentType:'text/javascript'}));
    await page.clock.install();await page.goto('http://127.0.0.1:'+server.address().port);
    await page.waitForFunction(()=>!!window.workIndicator);await page.clock.pauseAt(new Date(Date.now()+1000));
    for(const viewport of [{width:312,height:536,name:'outer'},{width:412,height:980,name:'inner'},{width:465,height:536,name:'outer-L'}]){
      await page.setViewportSize(viewport);
      for(const kind of ['call','message']){
        const samples=[];
        for(const stage of ['waiting','working','waiting-again']){
          const working=stage==='working';state.state=working?'在干活':'在等';
          const sample=await page.evaluate(({kind,working})=>{
            cancelAnimationFrame(animationFrame);
            setFaceState(working?'working':'waiting');
            const now=Date.now();workIndicator.update({state:working?'working':'waiting',now});
            workIndicator.update({state:working?'working':'waiting',now:now+1500});
            eyeExpressions.update({state:working?'working':'waiting',now:now+1500});
            document.body.classList.toggle('call-connected',kind==='call');document.body.classList.add('call-status-visible');
            const s=document.querySelector('#call-status');s.hidden=kind!=='call';s.textContent='已接通 · 自动听';
            const text='这句话的位置要固定，打字出现或收起，都留在这里。';
            if(kind==='call')faceMessages.showCallSubtitle(text,.2);
            else{faceMessages.clearCallSubtitle();faceMessages.showSubtitle(text,Date.now()/1000+5);}
            faceMessages.layout();
            const g=faceMessages.geometry(),caption=document.querySelector('#subtitle');
            return {caption:g.caption,status:g.callStatus,keyboard:workIndicator.diagnostic().mode,
              captionParent:caption.parentElement.id,statusParent:s.parentElement.id,
              layerZ:Number(getComputedStyle(caption.parentElement).zIndex),
              lens:[...document.querySelectorAll('clipPath path')].map(p=>p.getAttribute('d'))};
          },{kind,working});
          samples.push(sample);
          // Freeze the real opacity transition at its final frame for readable,
          // deterministic before/after screenshots; layout is untouched.
          await page.evaluate(()=>document.getAnimations().filter(a=>a.effect?.target?.id==='subtitle').forEach(a=>a.finish()));
          if(output)await page.screenshot({path:path.join(output,`${viewport.name}-${kind}-${stage}.png`)});
        }
        for(let i=1;i<samples.length;i++)for(const key of ['caption','status']){
          try{assert.deepEqual(samples[i][key],samples[0][key],`${viewport.name}/${kind}/${key} must not move by one pixel`);}
          catch(e){failures.push(e.message);}
        }
        try{assert.equal(samples[0].captionParent,samples[0].statusParent);assert.ok(samples[0].layerZ>10,'text must be in its own top overlay');}
        catch(e){failures.push(e.message);}
        report.push({viewport,kind,samples});
      }
    }
    assert.deepEqual(errors,[]);if(output)fs.writeFileSync(path.join(output,'geometry.json'),JSON.stringify({report,failures},null,2));
    console.log(JSON.stringify({cases:report.length,failures},null,2));assert.deepEqual(failures,[]);
  }finally{await browser.close();server.closeAllConnections();server.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});

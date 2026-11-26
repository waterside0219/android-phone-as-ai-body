const {chromium}=require(process.env.PLAYWRIGHT_PATH||'playwright');
const fs=require('node:fs'),http=require('node:http'),path=require('node:path'),assert=require('node:assert/strict');
const evidence=process.env.EVIDENCE_DIR;
const server=http.createServer((req,res)=>{
  const file=path.join(__dirname,req.url.split('?')[0]==='/'?'index.html':req.url.split('?')[0]);
  fs.readFile(file,(err,data)=>{if(err)return res.writeHead(404).end();res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript':file.endsWith('.html')?'text/html':'application/json');res.end(data);});
});
(async()=>{
  await new Promise(r=>server.listen(0,'127.0.0.1',r));
  const browser=await chromium.launch({headless:true});
  try{
    const page=await browser.newPage({viewport:{width:311,height:535}}),errors=[];
    page.on('pageerror',e=>errors.push(e.message));
    const state={state:'在等',camera_mode:'off',camera_active:false,presence:false,muted:true,off_mode:false};
    await page.route('http://127.0.0.1:8192/**',r=>r.fulfill({json:r.request().url().includes('/api/messages')?{messages:[],unread:0}:state,headers:{'Access-Control-Allow-Origin':'*'}}));
    await page.route('http://127.0.0.1:8191/**',r=>r.fulfill({body:'',contentType:'text/javascript'}));
    await page.clock.install();await page.goto('http://127.0.0.1:'+server.address().port);await page.clock.pauseAt(new Date(Date.now()+1000));
    assert.equal(await page.locator('.glasses-frame').count(),1,'one filled frame contains both lenses, the bridge and the short temples');
    assert.notEqual(await page.locator('.glasses-rim').first().evaluate(e=>getComputedStyle(e).fill),'none','the reference frame is a filled body, not a device-pixel thin outline');
    const frameStyle=await page.evaluate(()=>{
      const rim=document.querySelector('.glasses-rim'),box=rim.getBBox();
      return {ratio:box.width/box.height,vectorEffect:getComputedStyle(rim).vectorEffect,holes:(rim.getAttribute('d').match(/M/g)||[]).length-1,solid:[new DOMPoint(66,100),new DOMPoint(180,100),new DOMPoint(294,100)].map(p=>rim.isPointInFill(p))};
    });
    assert.ok(frameStyle.ratio>2.9&&frameStyle.ratio<3.1,'the whole frame follows the first reference panel proportions');
    assert.equal(frameStyle.holes,2);assert.deepEqual(frameStyle.solid,[true,true,true],'short temples and nose bridge have a solid body');
    assert.equal(frameStyle.vectorEffect,'none','filled frame thickness scales with the scene on the phone');
    for(const viewport of [{width:311,height:535},{width:376,height:857}]){
      await page.setViewportSize(viewport);
      const center=await page.evaluate(()=>{
        const now=Date.now()+10000;
        eyeExpressions.update({reaction:'normal',now});eyeExpressions.update({reaction:'normal',now:now+800});
        const rims=[...document.querySelectorAll('.glasses-rim')].map(x=>x.getBoundingClientRect());
        return {x:(rims[0].left+rims[0].right)/2,y:(rims[0].top+rims[0].bottom)/2,width:rims[0].width};
      });
      assert.ok(Math.abs(center.x-viewport.width/2)<.1,'glasses baseline is horizontally centered');
      assert.ok(Math.abs(center.y-viewport.height/2)<.1,'glasses baseline is vertically centered before anti-burn drift');
      const eye=await page.locator('.eye-shape').first().boundingBox();
      assert.ok(Math.abs(eye.width-22*1.15*viewport.width/360)<.2,'the whole scene enlarges the eye width by 15%');
      assert.ok(Math.abs(eye.height-54*1.15*viewport.width/360)<.2,'the whole scene enlarges the eye height by 15%');
    }
    const physicalCenter=await page.evaluate(()=>{
      const match=window.matchMedia;
      Object.defineProperty(screen,'width',{value:376,configurable:true});
      Object.defineProperty(screen,'height',{value:897,configurable:true});
      window.matchMedia=q=>q==='(display-mode: fullscreen)'?{matches:true}:match.call(window,q);
      window.dispatchEvent(new Event('resize'));
      const r=document.querySelector('.glasses-rim').getBoundingClientRect();
      const center=r.top+r.height/2+(screen.height-visualViewport.height);
      delete screen.width;delete screen.height;window.matchMedia=match;
      window.dispatchEvent(new Event('resize'));
      return center;
    });
    assert.ok(Math.abs(physicalCenter-897/2)<.1,'fullscreen inner display centers across the native top cutout too');
    const lateScreenCenter=await page.evaluate(()=>{
      const match=window.matchMedia;
      window.matchMedia=q=>q==='(display-mode: fullscreen)'?{matches:true}:match.call(window,q);
      Object.defineProperty(screen,'width',{value:465,configurable:true});Object.defineProperty(screen,'height',{value:535,configurable:true});
      window.dispatchEvent(new Event('resize'));
      Object.defineProperty(screen,'width',{value:376,configurable:true});Object.defineProperty(screen,'height',{value:897,configurable:true});
      const now=Date.now()+12000;eyeExpressions.update({reaction:'normal',now});eyeExpressions.update({reaction:'normal',now:now+800});
      const r=document.querySelector('.glasses-frame').getBoundingClientRect(),center=r.top+r.height/2+40;
      delete screen.width;delete screen.height;window.matchMedia=match;window.dispatchEvent(new Event('resize'));
      return center;
    });
    assert.ok(Math.abs(lateScreenCenter-897/2)<.1,'folding updates centering even when screen metrics settle after resize');
    await page.setViewportSize({width:311,height:535});
    const scaledMotion=await page.evaluate(()=>{
      const scene=document.querySelector('.glasses-scene'),rim=document.querySelector('.glasses-rim'),gaze=document.querySelector('.eye-gaze');
      let now=Date.now()+20000;
      const point=(node,x,y)=>{const p=new DOMPoint(x,y).matrixTransform(node.getScreenCTM());return [p.x,p.y];};
      const sample=width=>{
        scene.style.width=width;
        now+=3000;eyeExpressions.update({reaction:'normal',now});eyeExpressions.update({reaction:'normal',now:now+800});
        const base=point(rim,-48,-41);
        eyeExpressions.update({reaction:'heart',now:now+801});eyeExpressions.update({reaction:'heart',now:now+1126});
        const mid=point(rim,-48,-41);
        now+=3000;eyeExpressions.update({reaction:'normal',now});eyeExpressions.update({reaction:'normal',now:now+800});
        const lookStart=point(gaze,0,0);
        eyeExpressions.update({reaction:'normal',now:now+900,look:{x:1,y:1}});
        const lookEnd=point(gaze,0,0);
        return [...mid.map((v,i)=>v-base[i]),...lookEnd.map((v,i)=>v-lookStart[i])];
      };
      const result={base:sample('100%'),enlarged:sample('')};
      eyeExpressions.update({reaction:'blink',now:now+2000});
      return result;
    });
    for(let i=0;i<scaledMotion.base.length;i++)assert.ok(Math.abs(scaledMotion.enlarged[i]-scaledMotion.base[i]*1.15)<.05,'morph sway/rotation and gaze travel enlarge by the same 15%');
    const shapeNames=['normal','blink','half','smile','heart','squint','surprised','sleepy','thinking','working','stuck','angry','glint'];
    const results=await page.evaluate(names=>{
      let t=Date.now();
      const snap=()=>{
        const rims=[...document.querySelectorAll('.glasses-rim')].map(e=>e.getBoundingClientRect());
        const frameCenter=new DOMPoint(180,112).matrixTransform(document.querySelector('.glasses-frame').getScreenCTM());
        return {d:document.querySelector('.eye-shape').getAttribute('d'),rig:document.querySelector('#glasses-rig').getAttribute('transform'),rimCenterY:frameCenter.y,...eyeExpressions.diagnostic()};
      };
      const render=(name,delta=0,extra={})=>{t+=delta;eyeExpressions.update({reaction:name,now:t,...extra});return snap();};
      render('normal');render('normal',800);
      const transitions=[];
      for(const name of names){
        const from=snap(),start=render(name),mid=render(name,325),end=render(name,475);
        transitions.push({name,from,start,mid,end});
      }
      render('normal');render('normal',800);render('heart');const partial=render('heart',180),interrupt=render('half');
      return {transitions,partial,interrupt,points:Object.values(eyeExpressions.presets).map(p=>p.eyes.map(e=>e.length))};
    },shapeNames);
    for(const sizes of results.points)assert.deepEqual(sizes,[96,96]);
    for(const {name,from,start,mid,end} of results.transitions){
      assert.equal(start.d,from.d,name+': transition starts at the displayed outline');
      if(from.d!==end.d){assert.notEqual(mid.d,start.d,name+': intermediate outline exists');assert.notEqual(mid.d,end.d,name+': intermediate is not the destination');}
      assert.equal(end.name,name);assert.ok(end.transition>=400&&end.transition<=800);
    }
    assert.equal(results.interrupt.d,results.partial.d,'interruption continues from the visible outline');
    assert.notEqual(results.transitions[3].mid.rig,results.transitions[3].start.rig,'rims move gently with morph');
    assert.ok(results.transitions[0].end.rimCenterY-results.transitions[3].end.rimCenterY>4,'a smile visibly lifts the frame on the narrow cover viewport');
    const blink=await page.evaluate(()=>{
      let now=Date.now()+30000;eyeExpressions.update({reaction:'normal',now});eyeExpressions.update({reaction:'normal',now:now+800});
      const rim=document.querySelector('.glasses-rim'),before=rim.getBoundingClientRect().height;
      eyeExpressions.update({reaction:'normal',now:now+900,blink:true});eyeExpressions.update({reaction:'normal',now:now+1030,blink:true});
      let band=0;for(let y=0;y<112;y+=.05)if(rim.isPointInFill(new DOMPoint(122,y)))band+=.05;
      const matrix=rim.getScreenCTM();
      return {before,after:rim.getBoundingClientRect().height,eye:document.querySelector('.eye-blink').getAttribute('transform'),frame:getComputedStyle(rim).fill,rimWidth:band*Math.hypot(matrix.a,matrix.b),eyeColor:getComputedStyle(document.querySelector('.eye-shape')).fill,clip:document.querySelector('.lens-glint').getAttribute('clip-path')};
    });
    assert.ok(Math.abs(blink.before-blink.after)<1,'blink never flattens the rims');assert.match(blink.eye,/scale\(1 0\.0/);assert.match(blink.frame,/#glasses-frame-ink/);assert.notEqual(blink.frame,blink.eyeColor);assert.match(blink.clip,/url\(#lens-clip-/);
    const idle=await page.evaluate(()=>{
      presence=false;lastPresenceAt=Date.now();const values=[];
      for(let i=0;i<8;i++){nextDriftAt=0;const old={...drift};updateIdleDisplay();values.push({old,next:{...drift}});}
      lastPresenceAt=Date.now()-600001;updateIdleDisplay();const dim=restStage;
      lastPresenceAt=Date.now()-1800001;updateIdleDisplay();const doze=restStage,brightness=getComputedStyle(document.body).getPropertyValue('--rest-brightness');
      showControls();return {values,dim,doze,brightness,restored:restStage};
    });
    for(const {old,next} of idle.values){
      assert.ok(Math.hypot(next.x-old.x,next.y-old.y)>blink.rimWidth*2,'each drift exceeds twice the actual rim width');
      assert.ok(Math.hypot(next.x,next.y)<=9,'cover display drift remains close to center');
      if(next.x||next.y)assert.ok(Math.abs(Math.hypot(next.x,next.y)-7.5*1.15)<.01,'whole-face drift travel enlarges by the same 15%');
      if(old.x||old.y)assert.deepEqual(next,{x:0,y:0},'each off-center rest returns to the centered baseline');
    }
    assert.equal(idle.dim,'dim');assert.equal(idle.doze,'doze');assert.equal(Number(idle.brightness),0,'30 minutes extinguishes the complete face');assert.equal(idle.restored,'active');
    const routes=await page.evaluate(()=>{
      const render=(args)=>{eyeExpressions.update({now:Date.now()+60000,...args});return eyeExpressions.diagnostic().name;};
      return {waiting:render({state:'waiting'}),present:render({state:'waiting',present:true}),speaking:render({state:'working',speaking:true}),touch:['startled','hold','chin','kiss'].map(reaction=>render({reaction})),sequences:eyeExpressions.sequences};
    });
    assert.equal(routes.waiting,'normal');assert.equal(routes.present,'normal','presence alone leaves idle eyes normal');assert.equal(routes.speaking,'smile');assert.deepEqual(routes.touch,['heart','heart','heart','heart']);assert.ok(!Object.values(routes.sequences).flat().includes('heart'),'heart is touch-only');
    await page.reload();await page.mouse.click(155,200);await page.clock.runFor(1000);
    assert.equal(await page.evaluate(()=>eyeExpressions.diagnostic().name),'heart','real tap handler reaches heart');
    await page.clock.runFor(1900);assert.notEqual(await page.evaluate(()=>eyeExpressions.diagnostic().name),'heart','tap returns after 2.6 seconds');
    await page.mouse.move(155,200);await page.mouse.down();await page.clock.runFor(700);await page.mouse.up();await page.clock.runFor(1000);
    assert.equal(await page.evaluate(()=>eyeExpressions.diagnostic().name),'heart','released hold keeps its afterglow');
    await page.clock.runFor(1000);assert.notEqual(await page.evaluate(()=>eyeExpressions.diagnostic().name),'heart','released hold returns after 1.8 seconds');
    await page.mouse.down();await page.clock.runFor(700);await page.mouse.move(185,230);await page.mouse.up();await page.clock.runFor(30);
    assert.notEqual(await page.evaluate(()=>eyeExpressions.diagnostic().name),'heart','drag cancels hold without reviving heart on release');
    await page.evaluate(()=>{clearTimeout(blinkTimer);clearTimeout(blinkEndTimer);simple.classList.remove('blink');document.body.classList.remove('controls-visible');});
    if(evidence){
      fs.mkdirSync(evidence,{recursive:true});fs.writeFileSync(path.join(evidence,'local-behavior.json'),JSON.stringify({results,blink,idle,routes},null,2));
      for(const viewport of [{width:311,height:535,name:'outer'},{width:376,height:857,name:'inner'}]){
        await page.setViewportSize(viewport);
        for(const [i,name] of shapeNames.entries()){
          await page.evaluate(({name,i})=>{const now=Date.now()+100000+i*2000;eyeExpressions.update({reaction:name,now});eyeExpressions.update({reaction:name,now:now+800});}, {name,i});
          await page.screenshot({path:path.join(evidence,`local-${viewport.name}-${name}.png`)});
        }
        await page.evaluate(()=>{const now=Date.now()+200000;eyeExpressions.update({reaction:'normal',now});eyeExpressions.update({reaction:'normal',now:now+800});eyeExpressions.update({reaction:'heart',now:now+801});eyeExpressions.update({reaction:'heart',now:now+1126});});
        await page.screenshot({path:path.join(evidence,`local-${viewport.name}-mid-morph.png`)});
      }
    }
    assert.deepEqual(errors,[]);console.log('13 contours, continuous/interrupted morphs, silver rims, blink isolation, touch routes and shared rest/drift passed');
  }finally{await browser.close();server.close();}
})().catch(e=>{console.error(e);server.close();process.exitCode=1;});

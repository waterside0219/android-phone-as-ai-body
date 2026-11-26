// Local browser contract; no phone, accounts, microphone, or external service.
const {chromium}=require(process.env.PLAYWRIGHT_PATH || 'playwright');
const fs=require('node:fs'), http=require('node:http'), path=require('node:path'), assert=require('node:assert/strict');
const evidence=process.env.EVIDENCE_DIR;
const server=http.createServer((req,res)=>{
  const file=path.join(__dirname,decodeURIComponent(req.url.split('?')[0]==='/'?'/index.html':req.url.split('?')[0]));
  if(!file.startsWith(__dirname+'/')){res.writeHead(403).end();return;}
  fs.readFile(file,(error,data)=>{if(error){res.writeHead(404).end();return;}res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript':file.endsWith('.html')?'text/html':file.endsWith('.png')?'image/png':'application/json');res.end(data);});
});
(async()=>{
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const browser=await chromium.launch({headless:true});
  try{
    const page=await browser.newPage({viewport:{width:312,height:536}});
    const now=Date.now()/1000;
    const history=Array.from({length:30},(_,i)=>({id:i+1,at:now-60*(30-i),speaker:'AI',text:'已留存的第'+i+'句话'}));
    history[29]={id:30,at:now,speaker:'AI',text:'这是一句很长的字幕，用来核对文字始终放在下巴以下，分成最多两行轮换显示，无论在内屏还是外屏，都不压住眼睛、眼镜、耳朵和脸。'};
    let read=0,off=false;const replies=[];
    const state=()=>({state:'在等',camera_enabled:false,camera_mode:'off',camera_active:false,watch_active:true,closed_until:0,muted:true,manual_mute:true,off_mode:off,off_display:'eyes',touch_locked:false,subtitle:history.filter(m=>m.speaker==='AI').at(-1).text,subtitle_until:now+120,message_id:history.at(-1).id,latest_message:history.filter(m=>m.speaker==='AI').at(-1),unread:history.filter(m=>m.speaker==='AI'&&m.id>read).length});
    const messages=()=>({messages:history,read_through:read,unread:state().unread});
    await page.route('http://127.0.0.1:8192/**',async route=>{
      const request=route.request(),url=new URL(request.url());let response=state();
      if(url.pathname==='/api/messages'&&request.method()==='GET')response=messages();
      else if(url.pathname==='/api/messages/read'){read=request.postDataJSON().through;response=messages();}
      else if(url.pathname==='/api/messages'){
        const text=request.postDataJSON().text;replies.push(text);history.push({id:history.at(-1).id+1,at:Date.now()/1000,speaker:'我',text});history.splice(0,history.length-30);response=messages();
      }else if(url.pathname==='/api/settings'){off=request.postDataJSON().off_mode??off;response=state();}
      await route.fulfill({json:response,headers:{'Access-Control-Allow-Origin':'*'}});
    });
    await page.goto('http://127.0.0.1:'+server.address().port);
    await page.waitForSelector('#message-peek',{timeout:2000});
    await page.waitForFunction(()=>document.querySelector('#message-unread')&&!document.querySelector('#message-unread').hidden);
    await page.evaluate(()=>setMode('live2d'));
    await page.waitForFunction(()=>!!window.faceLive2D?.model);
    await page.click('#message-peek');
    await page.waitForSelector('#message-panel:not([hidden])');
    await page.waitForFunction(()=>document.querySelectorAll('#message-list li').length===30);
    assert.equal(await page.locator('#message-list li').count(),30);
    await page.waitForFunction(()=>document.querySelector('#message-unread').hidden);
    if(evidence)await page.screenshot({path:path.join(evidence,'3-outer-panel.png')});
    await page.evaluate(()=>document.querySelector('#message-list').scrollTop=0);
    const touch=await page.context().newCDPSession(page);
    const box=await page.locator('#message-list').boundingBox();
    const x=box.x+40,y=box.y+30;
    await touch.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x,y}]});
    for(let dy=15;dy<=105;dy+=15)await touch.send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:[{x,y:y+dy}]});
    await touch.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
    assert.equal(await page.locator('#message-panel').isHidden(),true,'swipe down closes panel');
    await page.evaluate(()=>showControls());await page.click('#settings-open');await page.click('#off-toggle');
    await page.click('#messages-open');
    await page.fill('#message-input','程序验收：全关模式仍能回复');
    await page.click('#message-send');
    await page.waitForFunction(()=>document.querySelector('#message-input').value==='');
    assert.equal(replies.at(-1),'程序验收：全关模式仍能回复');
    assert.match(await page.locator('#message-list li').last().innerText(),/我/);
    await page.click('#message-close');await page.evaluate(()=>showControls());await page.click('#settings-open');await page.click('#off-toggle');await page.click('#settings-close');
    const results=[];
    if(process.env.CALL_LAYOUT)await page.evaluate(()=>{
      document.body.classList.add('call-active');
      const status=document.querySelector('#call-status');status.hidden=false;status.textContent='已接通 · 自动听';
      document.querySelector('#call-controls').hidden=false;
      faceMessages.showCallSubtitle('电话里的这句话，也要始终放在下巴下面，不挡住眼睛和脸。');
    });
    for(const viewport of [{width:412,height:980,name:'inner'},{width:312,height:536,name:'outer'},{width:465,height:536,name:'outer-L-simulated'}]){
      await page.setViewportSize(viewport);
      for(const mode of ['eyes','live2d']){
        await page.evaluate(mode=>setMode(mode),mode);
        if(mode==='live2d')await page.waitForFunction(()=>!!window.faceLive2D?.drawables);
        await page.waitForFunction(()=>window.faceMessages?.geometry()?.caption?.height>0);
        await page.evaluate(()=>faceMessages.layout());
        const result=await page.evaluate(()=>faceMessages.geometry());
        assert.ok(result.caption.top>=result.forbidden.bottom+5,JSON.stringify({viewport,mode,result}));
        assert.ok(result.caption.height<=result.fontSize*1.55*2+18,'at most two subtitle lines');
        assert.ok(result.caption.right<=result.usableWidth,'inside usable screen');
        assert.ok(result.peek.width<=44,'unread indicator remains small');
        if(process.env.CALL_LAYOUT){assert.ok(result.callStatus.top>=result.forbidden.bottom,'call status avoids face');assert.ok(result.callStatus.bottom<viewport.height-42,'call status stays above controls');}
        results.push({viewport,mode,...result});
        if(evidence)await page.screenshot({path:path.join(evidence,(process.env.CALL_LAYOUT?'4-call-':'3-')+viewport.name+'-'+mode+'.png')});
      }
    }
    if(evidence)fs.writeFileSync(path.join(evidence,process.env.CALL_LAYOUT?'4-call-layout.json':'3-layout.json'),JSON.stringify(results,null,2));
    await page.evaluate(()=>{window.faceCall={busy:true,speaking:false,activityAt:Date.now()-301000,diagnostic:()=>({state:'connected'})};setMode('live2d');lastActiveAt=Date.now()-301000;updateIdleDisplay();});
    assert.equal(await page.evaluate(()=>mode),'eyes','idle reduces GPU work');
    assert.equal(await page.evaluate(()=>localStorage.getItem('face-mode')),'live2d','idle preserves preferred model');
    await page.evaluate(()=>showControls());
    assert.equal(await page.evaluate(()=>mode),'live2d','touch restores preferred model');
    console.log('30 messages, unread/read, full-off reply, two-line face exclusion in inner/outer/L viewport: passed');
  }finally{await browser.close();server.close();}
})().catch(error=>{console.error(error);server.close();process.exitCode=1;});

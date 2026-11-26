const {chromium}=require(process.env.PLAYWRIGHT_PATH||'playwright');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
(async()=>{const browser=await chromium.launch({headless:true});try{
  const page=await browser.newPage({viewport:{width:312,height:536}}),errors=[];page.on('pageerror',e=>errors.push(String(e)));
  await page.route('**/*',route=>route.abort());
  await page.setContent(fs.readFileSync(path.join(__dirname,'index.html'),'utf8').replace(/<script[\s\S]*?<\/script>/g,''));
  await page.clock.install();
  await page.evaluate(()=>{window.API='http://127.0.0.1:8192';window.faceExclusion=()=>({left:40,top:30,right:270,bottom:240});});
  await page.addScriptTag({path:path.join(__dirname,'messages.js')});
  const full='这一段没有适合断句的标点所以需要在同一句内部按声音进度换页'.repeat(5)+'最后这些字也必须完整显示';
  await page.evaluate(full=>faceMessages.showCallSubtitle(full,0),full);
  const first=await page.locator('#subtitle').textContent();await page.clock.runFor(6500);
  assert.equal(await page.locator('#subtitle').textContent(),first,'paused playback must not turn subtitle pages');
  assert.equal(await page.locator('#subtitle').evaluate(e=>e.classList.contains('visible')),true,'call captions are not cut off after five seconds');
  const pages=await page.evaluate(full=>{
    const pages=[];for(let i=0;i<=500;i++){
      faceMessages.showCallSubtitle(full,i/500);faceMessages.update({subtitle:'尚未说到的整段文字',subtitle_until:Date.now()/1000+120});
      const text=document.querySelector('#subtitle').textContent;
      if(text!==pages.at(-1))pages.push(text);
    }return pages;
  },full);
  assert.equal(pages.join('').replaceAll('\n',''),full,'every character including final page must appear');
  assert.ok(pages.length>2);
  const geometry=await page.evaluate(()=>({layout:faceMessages.geometry(),height:document.querySelector('#subtitle').getBoundingClientRect().height}));
  assert.ok(geometry.height<=geometry.layout.fontSize*3.1+18,'still at most two visible lines');
  await page.evaluate(()=>{faceMessages.clearCallSubtitle();faceMessages.showSubtitle('普通消息仍然五秒淡出',Date.now()/1000+120);});
  await page.clock.runFor(5100);assert.equal(await page.locator('#subtitle').evaluate(e=>e.classList.contains('visible')),false);
  await page.evaluate(()=>{document.body.classList.add('call-connected','call-attention');const s=document.querySelector('#call-status');s.hidden=false;s.textContent='连接中断，正在自动重连…';});
  assert.equal(await page.locator('#call-status').evaluate(e=>getComputedStyle(e).visibility),'visible','recovery must override the hidden in-call status');
  assert.deepEqual(errors,[]);console.log('PASS clock-driven call captions: no five-second loss, complete two-line pages, ordinary expiry, visible recovery warning');
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exitCode=1;});

const API='http://127.0.0.1:8192';
const workScript=document.createElement('script');workScript.src='work-indicator.js?v=20261029-j1';document.head.append(workScript);
const pageRelease=fetch('release.json',{cache:'no-store'}).then(r=>r.ok?r.json():{version:'bootstrap'}).then(r=>r.version||'bootstrap').catch(()=>'bootstrap');
const canvas=document.querySelector('#live');
const simple=document.querySelector('#simple');
const glass=document.querySelector('#glass');
const button=document.querySelector('#switch');
const stateLabel=document.querySelector('#state');
const subtitle=document.querySelector('#subtitle');
const closeHours=document.querySelector('#close-hours');
let live2d,loading=false;
let mode='eyes',faceState='waiting',touchLocked=false,night=false;
let pointer={x:0,y:0},touchLook={x:0,y:0},touchLookUntil=0,presence=false,serverLook={x:0,y:0};
let lookInterval=1000;
let cooling=false,coolingTimer=0,coolingSince=0,animationFrame=0;
let serverClosedUntil=0,localClosedUntil=Number(localStorage.getItem('closed-until'))||0,lockOverride=null,lastClosed;
let speakingUntil=0, allOff=false, muted=true, offPriorMode='eyes';
let blinkOpen=1,blinkTimer,blinkEndTimer,reactionTimer,lastFrame=0;
let lastTextLayout=0;
let lastActiveAt=Date.now(),idleMode=false,lastPresenceAt=Date.now(),restStage='active',nextDriftAt=Date.now()+120000,drift={x:0,y:0};
const stateNames={thinking:'在想',working:'在干活',waiting:'在等',stuck:'卡住了'};
const clamp=(value,min=-1,max=1)=>Math.max(min,Math.min(max,value));
let controlsTimer;
let callStatusTimer=0,callConnected=false;
let renderSampleAt=0,renderFrames=0,renderMaxGap=0,renderMetrics=null;
let pendingControls={},latestSettings={},controlsRevision=0,savingControls=false;
try{pendingControls=JSON.parse(localStorage.getItem('pending-controls')||'{}');}catch{}
function publishSettings(data){
  latestSettings=data;
  const effective={...data,...pendingControls};
  if('camera_enabled' in pendingControls || 'camera_mode' in pendingControls || 'off_mode' in pendingControls)effective.camera_active=effective.camera_allowed=false;
  if('off_mode' in pendingControls)effective.off_mode=data.off_mode===true||pendingControls.off_mode===true;
  if('muted' in pendingControls)effective.muted=data.muted===true||pendingControls.muted===true;
  if(effective.off_mode){effective.camera_active=effective.camera_allowed=false;effective.muted=true;}
  effective.controls_pending=Object.keys(pendingControls).length>0;
  applyControls(effective);
  window.dispatchEvent(new CustomEvent('face-settings',{detail:effective}));
}
function saveControls(patch){
  Object.assign(pendingControls,patch);controlsRevision++;
  localStorage.setItem('pending-controls',JSON.stringify(pendingControls));
  publishSettings(latestSettings);
  return syncControls();
}
async function syncControls(){
  if(savingControls||!Object.keys(pendingControls).length)return;
  savingControls=true;
  const patch={...pendingControls};
  try{
    const data=await postJSON('/api/settings',patch);
    if(data){
      for(const key of Object.keys(patch))if(pendingControls[key]===patch[key])delete pendingControls[key];
      controlsRevision++;localStorage.setItem('pending-controls',JSON.stringify(pendingControls));publishSettings(data);
    }
  }finally{savingControls=false;}
}
function showControls(){lastActiveAt=lastPresenceAt=Date.now();updateIdleDisplay();document.body.classList.add('controls-visible');clearTimeout(controlsTimer);controlsTimer=setTimeout(()=>document.body.classList.remove('controls-visible'),5000);}
document.addEventListener('pointerdown',showControls,{capture:true});
function syncCallAppearance(){
  const connected=window.faceCall?.diagnostic().state==='connected';
  if(connected===callConnected)return;
  callConnected=connected;
  clearTimeout(callStatusTimer);
  document.body.classList.remove('call-status-visible');
  document.body.classList.toggle('call-connected',connected);
}
// The call client owns readiness; its controls change on connect/end. Observe
// that existing lifecycle instead of maintaining another call state machine.
const callObserver=new MutationObserver(syncCallAppearance);
callObserver.observe(document.querySelector('#call-controls'),{attributes:true,subtree:true,attributeFilter:['hidden']});
document.addEventListener('pointerdown',()=>{
  if(!callConnected)return;
  document.body.classList.add('call-status-visible');
  clearTimeout(callStatusTimer);
  callStatusTimer=setTimeout(()=>document.body.classList.remove('call-status-visible'),3000);
},{capture:true});
const settingsPanel=document.querySelector('#settings-panel');
window.closeSettings=()=>{settingsPanel.hidden=true;document.body.classList.remove('settings-open');};
document.querySelector('#settings-open').addEventListener('click',()=>{settingsPanel.hidden=false;document.body.classList.add('settings-open');});
document.querySelector('#settings-close').addEventListener('click',closeSettings);
settingsPanel.addEventListener('pointerdown',event=>event.stopPropagation());
settingsPanel.addEventListener('click',event=>{if(event.target===settingsPanel)closeSettings();});
document.querySelector('#call-link').addEventListener('click',closeSettings);
window.addEventListener('face-call-active',closeSettings);
window.addEventListener('keydown',event=>{if(event.key==='Escape')closeSettings();});

function animate(now){
  if(cooling)return;
  if(!renderSampleAt)renderSampleAt=now;
  else renderFrames++;
  if(lastFrame)renderMaxGap=Math.max(renderMaxGap,now-lastFrame);
  if(now-renderSampleAt>=5000){
    renderMetrics={fps:1000*renderFrames/(now-renderSampleAt),max_gap_ms:renderMaxGap,window_ms:now-renderSampleAt,sampled_at:Date.now()};
    renderFrames=0;renderMaxGap=0;renderSampleAt=now;
  }
  updateIdleDisplay();
  const dt=Math.min(.05,(now-(lastFrame||now))/1000);lastFrame=now;
  const target=isClosed()?{x:0,y:0}:now<touchLookUntil?touchLook:(presence?serverLook:{x:0,y:0});
  const gazeRate=-Math.log(.05)/Math.max(.3,lookInterval/1000*5/6);
  const smoothing=1-Math.exp(-dt*(presence&&now>=touchLookUntil?gazeRate:13));
  pointer.x+=(target.x-pointer.x)*smoothing;pointer.y+=(target.y-pointer.y)*smoothing;
  window.workIndicator?.update({state:faceState,now:Date.now(),closed:isClosed(),quiet:allOff,hidden:mode!=='eyes'||document.visibilityState!=='visible'});
  window.eyeExpressions?.update({state:faceState,present:presence,speaking:Date.now()<speakingUntil||window.faceCall?.speaking===true,reaction:simple.dataset.reaction,now:Date.now(),blink:simple.classList.contains('blink'),closed:isClosed(),quiet:allOff,look:pointer});
  if(mode==='live2d'&&live2d&&document.visibilityState==='visible')live2d.frame(now,{
    look:presence||now<touchLookUntil?pointer:null,reaction:simple.dataset.reaction,closed:isClosed(),muted,
    blink:presence||now<touchLookUntil?blinkOpen:null,
  });
  if(now-lastTextLayout>150){window.faceMessages?.layout();lastTextLayout=now;}
  animationFrame=requestAnimationFrame(animate);
}
function setCooling(value){
  if(cooling===value)return;
  live2d?.suspend();
  cooling=value;document.body.classList.toggle('cooling',cooling);
  clearInterval(coolingTimer);
  if(cooling){
    renderSampleAt=renderFrames=renderMaxGap=0;renderMetrics=null;
    coolingSince=Date.now();cancelAnimationFrame(animationFrame);
    clearTimeout(blinkTimer);clearTimeout(blinkEndTimer);simple.classList.remove('blink');
    const render=()=>{
      drift={x:drift.x===1?-1:1,y:0};
      simple.style.setProperty('--drift-x',drift.x+'px');simple.style.setProperty('--drift-y','0px');
      window.workIndicator?.update({cooling:true});
      window.eyeExpressions?.update({cooling:true,now:Date.now()});window.faceMessages?.layout();
    };
    render();coolingTimer=setInterval(render,4000);
  }else{
    const duration=Date.now()-coolingSince;lastPresenceAt+=duration;lastActiveAt+=duration;
    lastFrame=0;scheduleBlink();animationFrame=requestAnimationFrame(animate);
  }
}
function faceExclusion(){
  if(mode==='live2d')return live2d?.exclusion()||null;
  const eyes=[...Array.from(document.querySelectorAll('.glasses-rim,.glasses-temple,.outfit-decoration[data-active=true] path'),eye=>eye.getBoundingClientRect()),...(window.workIndicator?.bounds()||[])];
  return {left:Math.min(...eyes.map(r=>r.left))-10,top:Math.min(...eyes.map(r=>r.top))-10,
          right:Math.max(...eyes.map(r=>r.right))+10,bottom:Math.max(...eyes.map(r=>r.bottom))+10};
}
async function startModel(){
  const {loadV2}=await import('./live2d-v2.js?v=20261029-h1');
  live2d=await loadV2(canvas);window.faceLive2D=live2d;
  if(mode!=='live2d'||cooling||document.visibilityState!=='visible')live2d.suspend();
}
document.addEventListener('visibilitychange',()=>live2d?.suspend());
window.addEventListener('pagehide',event=>{if(event.persisted){live2d?.suspend();return;}live2d?.dispose();live2d=null;window.faceLive2D=null;});
function setMode(next,remember=true){
  if(mode!==next)live2d?.suspend();
  mode=next;if(remember){lastActiveAt=Date.now();idleMode=false;if(!allOff)localStorage.setItem('face-mode',next);}document.body.classList.toggle('live2d',next==='live2d');button.textContent=next==='live2d'?'松石绿眼睛':'Live2D';
  if(next==='live2d'&&!live2d&&!loading){loading=true;startModel().catch(error=>{console.error(error);setMode('eyes');stateLabel.textContent='Live2D加载失败';}).finally(()=>{loading=false;});}
}
function updateIdleDisplay(){
  if(cooling)return;
  const now=Date.now();
  if(presence)lastPresenceAt=now;
  const absent=now-lastPresenceAt,nextStage=absent>=1800000?'doze':absent>=600000?'dim':'active';
  if(nextStage!==restStage){
    restStage=nextStage;document.body.dataset.restStage=restStage;
    pageRelease.then(version=>postJSON('/api/ready',{version,display:pageDisplay()}));
  }
  if(now>=nextDriftAt){
    const angle=Math.random()*Math.PI*2;
    const distance=7.5*Number(getComputedStyle(simple).getPropertyValue('--eye-scale'));
    const next=drift.x||drift.y?{x:0,y:0}:{x:Math.cos(angle)*distance,y:Math.sin(angle)*distance};
    drift=next;simple.style.setProperty('--drift-x',drift.x+'px');simple.style.setProperty('--drift-y',drift.y+'px');
    nextDriftAt=now+120000+Math.random()*120000;
  }
  if(presence||Date.now()<speakingUntil||window.faceCall?.speaking)lastActiveAt=Date.now();
  lastActiveAt=Math.max(lastActiveAt,window.faceCall?.activityAt||0);
  if(!allOff&&Date.now()-lastActiveAt>=300000&&mode==='live2d'){
    idleMode=true;setMode('eyes',false);
  }else if(idleMode&&Date.now()-lastActiveAt<300000&&!allOff){
    idleMode=false;setMode(localStorage.getItem('face-mode')==='live2d'?'live2d':'eyes',false);
  }
}
function normalizeState(state){return({thinking:'thinking','在想':'thinking','想':'thinking',working:'working','在干活':'working','干活':'working',waiting:'waiting','在等':'waiting','等待':'waiting',stuck:'stuck','卡住了':'stuck','卡住':'stuck'})[state]||'waiting';}
window.setFaceState=state=>{faceState=normalizeState(state);document.body.dataset.state=faceState;updateStateLabel();};
function updateStateLabel(){
  if(allOff)stateLabel.textContent='上班模式 · 安静陪着';
  else if(touchLocked)stateLabel.textContent='触摸已锁';
  else if(isClosed())stateLabel.textContent='闭眼休息中';
  else stateLabel.textContent=stateNames[faceState]+(night?' · 夜间':'');
}
function parseTime(value){
  if(value===null||value===undefined||value===''||value===0||value==='0')return 0;
  if(typeof value==='string'&&!/^\d+(\.\d+)?$/.test(value)){const parsed=Date.parse(value);return Number.isFinite(parsed)?parsed:0;}
  const number=Number(value);if(!Number.isFinite(number)||number<=0)return 0;return number<1e12?number*1000:number;
}
function isClosed(){return Date.now()<Math.max(serverClosedUntil,localClosedUntil);}
function updateClosed(){
  const closed=isClosed();document.body.classList.toggle('closed',closed);
  if(closed){simple.classList.remove('blink');setReaction(null);}else blinkOpen=1;
  updateStateLabel();
  if(closed!==lastClosed){lastClosed=closed;window.dispatchEvent(new CustomEvent('face-closed-change',{detail:{closed}}));}
}
window.addEventListener('face-vision-presence',event=>{
  const face=event.detail;presence=face.present;serverLook=face.look;
  if(Number.isFinite(face.interval_ms)&&face.interval_ms>0)lookInterval=Math.min(10000,face.interval_ms);
  document.body.classList.toggle('vision-awake',face.looking===true);
});
function updateNight(){const hour=new Date().getHours();night=hour>=23||hour<7;document.body.classList.toggle('night',night);updateStateLabel();}
function setReaction(name,duration=0){clearTimeout(reactionTimer);if(name)simple.dataset.reaction=name;else delete simple.dataset.reaction;if(name&&duration)reactionTimer=setTimeout(()=>delete simple.dataset.reaction,duration);}
function scheduleBlink(){
  clearTimeout(blinkTimer);clearTimeout(blinkEndTimer);
  if(cooling)return;
  blinkTimer=setTimeout(()=>{
    if(!isClosed()&&!['hold','chin'].includes(simple.dataset.reaction)){
      simple.classList.add('blink');blinkOpen=0;
      blinkEndTimer=setTimeout(()=>{simple.classList.remove('blink');blinkOpen=1;scheduleBlink();},night?240:135);
    }else scheduleBlink();
  },(night?4800:2200)+Math.random()*(night?5200:4300));
}
function lookAt(clientX,clientY){
  if(isClosed())return;
  const rect=glass.getBoundingClientRect();touchLook={x:clamp((clientX-rect.left)/rect.width*2-1),y:clamp((clientY-rect.top)/rect.height*2-1)};touchLookUntil=performance.now()+4000;
}
async function postJSON(path,body,options={}){
  const controller=options.controller||new AbortController(),timeout=setTimeout(()=>controller.abort(),5000);
  try{const response=await fetch(API+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),keepalive:options.keepalive!==false,signal:controller.signal});if(!response.ok)throw Error('HTTP '+response.status);return await response.json();}
  catch(error){console.warn('本机接口暂时不可用',error);return false;}
  finally{clearTimeout(timeout);}
}
function sendTouch(kind){return allOff?Promise.resolve(false):postJSON('/api/touch',{kind});}
function setTouchLocked(value){if(touchLocked!==!!value)setReaction(null);touchLocked=!!value;document.body.classList.toggle('touch-locked',touchLocked);updateStateLabel();}
function toggleTouchLock(){
  const value=!touchLocked;lockOverride={value,until:Date.now()+1600};setTouchLocked(value);sendTouch('two_finger_lock');
}
async function toggleClosed(){
  const hours=isClosed()?0:Number(closeHours.value);
  if(hours){localClosedUntil=Date.now()+hours*3600000;localStorage.setItem('closed-until',localClosedUntil);updateClosed();}
  const result=await postJSON('/api/eyes',{hours});
  if(result){serverClosedUntil=parseTime(result.closed_until);localClosedUntil=0;localStorage.setItem('closed-until',serverClosedUntil);}
  updateClosed();
}
async function pollFace(){
  syncControls();
  const revision=controlsRevision;
  const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),1400);
  try{
    const response=await fetch(API+'/api/face',{cache:'no-store',signal:controller.signal});if(!response.ok)throw Error('HTTP '+response.status);
    const data=await response.json();
    if(revision!==controlsRevision)return;
    if(data.release){
      const loaded=await pageRelease;
      if(data.release!==loaded){if(!Object.keys(pendingControls).length)location.reload();return;}
      if(cooling||lastFrame>0&&performance.now()-lastFrame<2000)postJSON('/api/ready',{version:loaded,display:pageDisplay()});
    }
    publishSettings(data);
    if(data.state!==undefined)setFaceState(data.state);
    if(!lockOverride||Date.now()>lockOverride.until){lockOverride=null;setTouchLocked(data.touch_locked);}
    serverClosedUntil=parseTime(data.closed_until);
    if(!localClosedUntil)localStorage.setItem('closed-until',serverClosedUntil);
    presence=data.presence===true||data.presence===1||data.presence==='true';
    if(data.look&&Number.isFinite(Number(data.look.x))&&Number.isFinite(Number(data.look.y)))serverLook={x:clamp(Number(data.look.x)),y:clamp(Number(data.look.y))};
    speakingUntil=parseTime(data.speaking_until);
    window.faceMessages?.update(data);
    updateClosed();
  }catch(error){console.warn('保留当前玻璃脸，轮询稍后重试',error);}
  finally{clearTimeout(timeout);setTimeout(pollFace,500);}
}
function pageDisplay(){return {
  render:renderMetrics&&Date.now()-renderMetrics.sampled_at<10000?renderMetrics:null,vision:window.faceVision?.diagnostic(),
  expression:window.eyeExpressions?.diagnostic(),rest_stage:restStage,absent_ms:Math.max(0,Date.now()-lastPresenceAt),drift,settings_open:!settingsPanel.hidden,controls_visible:document.body.classList.contains('controls-visible'),unread_dot:!document.querySelector('#message-unread').hidden,mode,visible:document.visibilityState==='visible',viewport:{width:innerWidth,height:innerHeight},layout:window.faceMessages?.geometry(),subtitle:{visible:subtitle.classList.contains('visible'),opacity:Number(getComputedStyle(subtitle).opacity),text:subtitle.textContent},
  call:window.faceCall?.diagnostic(),camera_active:window.faceVision?.streamAlive===true,camera_stream_alive:window.faceVision?.streamAlive===true,detector_ready:window.faceVision?.active===true,camera_started_at:window.faceVision?.startedAt||0,camera_probe_sleeping:window.faceVision?.probeSleeping===true,message_panel:document.querySelector('#message-panel')?.hidden===false,
};}
document.addEventListener('visibilitychange',()=>pageRelease.then(version=>postJSON('/api/ready',{version,display:pageDisplay()})));
function applyControls(data){
  setCooling(data.thermal_hold===true);
  muted=data.muted===true;
  const wasOff=allOff;allOff=data.off_mode===true;
  if(allOff&&!wasOff){offPriorMode=mode;setMode('eyes');setReaction(null);}
  else if(!allOff&&wasOff)setMode(offPriorMode);
  document.body.classList.toggle('all-off',allOff);
  document.body.classList.toggle('black-screen',allOff&&data.off_display==='black');
  const muteButton=document.querySelector('#mute-toggle');
  muteButton.textContent=('muted' in pendingControls||'off_mode' in pendingControls)?'声音：待确认':muted?'声音：关':'声音：开';muteButton.setAttribute('aria-pressed',String(muted));muteButton.disabled=allOff||data.controls_pending;
  document.querySelector('#off-toggle').textContent='off_mode' in pendingControls?'全关：待确认':allOff?'恢复':'全关';
  document.querySelector('#off-toggle').disabled='off_mode' in pendingControls;
  document.querySelector('#off-toggle').setAttribute('aria-pressed',String(allOff));
  document.querySelector('#off-display').value=data.off_display||'eyes';
  button.disabled=allOff;
  updateStateLabel();
}
for(const id of ['control-bar','off-display-control'])document.querySelector('#'+id).addEventListener('pointerdown',event=>event.stopPropagation());
document.querySelector('#mute-toggle').addEventListener('click',()=>saveControls({muted:!muted}));
document.querySelector('#off-toggle').addEventListener('click',()=>saveControls({off_mode:!allOff}));
document.querySelector('#off-display').addEventListener('change',event=>saveControls({off_display:event.target.value}));
button.addEventListener('click',event=>{event.stopPropagation();setMode(mode==='live2d'?'eyes':'live2d');});
button.addEventListener('pointerdown',event=>event.stopPropagation());
document.querySelector('#close-control').addEventListener('pointerdown',event=>event.stopPropagation());
closeHours.addEventListener('change',()=>{if(!isClosed())toggleClosed();else postJSON('/api/eyes',{hours:Number(closeHours.value)}).then(result=>{if(result){serverClosedUntil=parseTime(result.closed_until);localClosedUntil=0;localStorage.setItem('closed-until',serverClosedUntil);updateClosed();}});});
addEventListener('contextmenu',event=>event.preventDefault());

const pointers=new Map();
let gesture=null,holdTimer,sleepTimer,twoTimer;
let pendingTap=null,tapTimer;
function clearSingleTimers(){clearTimeout(holdTimer);clearTimeout(sleepTimer);}
function clearAllTimers(){clearSingleTimers();clearTimeout(twoTimer);}
window.addEventListener('face-call-active',()=>{clearAllTimers();clearTimeout(tapTimer);pendingTap=null;pointers.clear();gesture=null;setReaction(null);});
function startTwoFingerTimer(){
  clearTimeout(twoTimer);
  twoTimer=setTimeout(()=>{if(gesture&&pointers.size>=2){gesture.two=true;clearSingleTimers();setReaction(null);toggleTouchLock();}},2000);
}
glass.addEventListener('pointerdown',event=>{
  event.preventDefault();if(allOff)return;try{glass.setPointerCapture(event.pointerId);}catch{}
  const ear=mode==='live2d'&&!touchLocked&&!isClosed()?live2d?.hitEar(event.clientX,event.clientY):null;
  pointers.set(event.pointerId,{x:event.clientX,y:event.clientY,startX:event.clientX,startY:event.clientY,started:Date.now(),ear});
  if(pointers.size===1){clearAllTimers();gesture={multi:false,moved:false,hold:false,sleep:false,two:false,travelX:0,lastChin:0};if(!touchLocked&&!ear)lookAt(event.clientX,event.clientY);
    if(ear)live2d.pokeEar(ear);
    if(!touchLocked&&!ear){
      if(!isClosed())holdTimer=setTimeout(()=>{if(gesture&&pointers.size===1&&!gesture.moved){gesture.hold=true;setReaction('hold');sendTouch('hold');}},350);
      sleepTimer=setTimeout(()=>{if(gesture&&pointers.size===1&&!gesture.moved&&!gesture.multi){gesture.sleep=true;setReaction(null);toggleClosed();}},3000);
    }
  }else{
    gesture.multi=true;gesture.moved=true;clearSingleTimers();setReaction(null);
    if(pointers.size===2)startTwoFingerTimer();else clearTimeout(twoTimer);
  }
});
glass.addEventListener('pointermove',event=>{
  const previous=pointers.get(event.pointerId);if(!previous||!gesture)return;
  const dx=event.clientX-previous.x,dy=event.clientY-previous.y;
  pointers.set(event.pointerId,{...previous,x:event.clientX,y:event.clientY});
  if(gesture.multi||touchLocked||previous.ear)return;
  const totalX=event.clientX-previous.startX,totalY=event.clientY-previous.startY;
  if(Math.hypot(totalX,totalY)>12){gesture.moved=true;clearSingleTimers();if(gesture.hold)setReaction(null);}
  if(!isClosed()&&Math.abs(totalX)>Math.abs(totalY)*1.2)lookAt(event.clientX,event.clientY);
  gesture.travelX+=Math.abs(dx);
  const rect=glass.getBoundingClientRect(),lowerHalf=event.clientY>rect.top+rect.height/2;
  if(!isClosed()&&lowerHalf&&gesture.travelX>34&&Math.abs(totalX)>18&&Math.abs(totalX)>Math.abs(totalY)*1.2&&Date.now()-gesture.lastChin>650){
    gesture.moved=true;gesture.lastChin=Date.now();setReaction('chin',2400);sendTouch('chin');
  }
});
function endPointer(event,cancelled){
  const current=pointers.get(event.pointerId);if(!current||!gesture)return;
  const wasMulti=gesture.multi;const finished=gesture;pointers.delete(event.pointerId);clearSingleTimers();
  if(pointers.size<2)clearTimeout(twoTimer);
  if(!cancelled&&!wasMulti&&!touchLocked&&!current.ear&&!finished.sleep&&!finished.hold&&!finished.moved&&Date.now()-current.started<500){
    lookAt(event.clientX,event.clientY);setReaction('startled',2600);
    if(pendingTap&&Date.now()-pendingTap.at<=320&&Math.hypot(event.clientX-pendingTap.x,event.clientY-pendingTap.y)<32){
      clearTimeout(tapTimer);pendingTap=null;window.dispatchEvent(new CustomEvent('face-look-now'));
    }else{
      if(pendingTap){clearTimeout(tapTimer);sendTouch('tap');}
      pendingTap={at:Date.now(),x:event.clientX,y:event.clientY};
      tapTimer=setTimeout(()=>{pendingTap=null;if(!allOff&&!window.faceCall?.active)sendTouch('tap');},320);
    }
  }
  if(finished.hold)setReaction(cancelled||finished.sleep||finished.moved||wasMulti||touchLocked||isClosed()?null:'hold',1800);
  if(pointers.size===0){clearAllTimers();gesture=null;}
}
glass.addEventListener('pointerup',event=>endPointer(event,false));
glass.addEventListener('pointercancel',event=>endPointer(event,true));

publishSettings({});setFaceState('waiting');updateClosed();updateNight();setInterval(updateNight,60000);scheduleBlink();animationFrame=requestAnimationFrame(animate);pollFace();
if(!allOff&&(new URLSearchParams(location.search).get('mode')||localStorage.getItem('face-mode'))==='live2d')setMode('live2d');

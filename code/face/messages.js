// The phone service owns history/read receipts; this page owns presentation only.
(() => {
  const caption=document.querySelector('#subtitle'), peek=document.querySelector('#message-peek');
  const dot=document.querySelector('#message-unread');
  const panel=document.querySelector('#message-panel'), list=document.querySelector('#message-list');
  const input=document.querySelector('#message-input'), result=document.querySelector('#message-result');
  let revision='',text='',expiry=0,segments=[],segmentIndex=0,segmentTimer,fadeTimer,callProgress=null;
  let seenThrough=0,historyVersion=-1,fetching=false,captionKey='',geometry=null;
  let swipe=null,callCaption=null,lastData=null,noticeText='',noticeUntil=0;
  const rect=r=>({left:r.left,top:r.top,right:r.right,bottom:r.bottom,width:r.width,height:r.height});

  function splitText(value,width,fontSize){
    const measure=document.createElement('canvas').getContext('2d');
    measure.font=`500 ${fontSize}px system-ui`;
    const lines=[];let line='';
    for(const character of Array.from(value)){
      if(character==='\n'){lines.push(line);line='';continue;}
      if(line&&measure.measureText(line+character).width>width){lines.push(line);line='';}
      line+=character;
    }
    if(line)lines.push(line);
    const pages=[];for(let i=0;i<lines.length;i+=2)pages.push(lines.slice(i,i+2).join('\n'));
    return pages;
  }
  function drawSegment(){
    clearTimeout(segmentTimer);
    if(callProgress!==null)segmentIndex=Math.min(Math.max(0,segments.length-1),Math.floor(callProgress*segments.length));
    const value=segments[segmentIndex]||'';if(caption.textContent!==value)caption.textContent=value;
    if(callProgress===null&&segments.length>1&&Date.now()<expiry){
      const delay=Math.max(2400,Array.from(caption.textContent).length*300);
      segmentTimer=setTimeout(()=>{segmentIndex=(segmentIndex+1)%segments.length;drawSegment();},delay);
    }
  }
  function layout(){
    const width=innerWidth,height=innerHeight;
    // HyperOS currently grants 810x1392 beside the cover cutout. If it grants
    // the entire 1208x1392 L-shaped panel, keep text in the same clear slab.
    const usableWidth=width/height>.75&&width/height<1.1?Math.min(width,height*810/1392):width;
    document.documentElement.style.setProperty('--usable-width',usableWidth+'px');
    document.documentElement.style.setProperty('--panel-height',(visualViewport?.height||height)+'px');
    const callStatus=document.querySelector('#call-status');
    const forbidden=typeof faceExclusion==='function'?faceExclusion():null;
    if(!forbidden){caption.style.visibility='hidden';peek.style.visibility='hidden';if(callStatus)callStatus.style.visibility='hidden';return;}
    caption.style.visibility='';peek.style.visibility='';
    const margin=12,fontSize=Math.max(16,Math.min(23,usableWidth*.052));
    // Anchor to the static stage, not animated head/keyboard bounds. Both kinds
    // of captions share this overlay and reserve two lines for a fixed status row.
    const stage=document.querySelector('#face').getBoundingClientRect();
    const top=Math.round(stage.top+stage.height*.64);
    const captionWidth=usableWidth-margin*2;
    Object.assign(caption.style,{left:margin+'px',top:top+'px',width:captionWidth+'px',fontSize:fontSize+'px'});
    const key=[text,Math.floor(captionWidth),fontSize].join('|');
    if(key!==captionKey){captionKey=key;segments=splitText(text,captionWidth-24,fontSize);segmentIndex=Math.min(segmentIndex,Math.max(0,segments.length-1));drawSegment();}
    if(callStatus){callStatus.style.visibility='';callStatus.style.top=(top+fontSize*3.1+20)+'px';}
    geometry={forbidden,callStatus:callStatus&&!callStatus.hidden?rect(callStatus.getBoundingClientRect()):null,caption:rect(caption.getBoundingClientRect()),peek:rect(peek.getBoundingClientRect()),usableWidth,fontSize};
  }
  function showSubtitle(value,until,progress=null){
    const next=value+'|'+until+'|'+(progress!==null);callProgress=progress;
    if(next===revision){if(progress!==null)drawSegment();return;}revision=next;
    clearTimeout(fadeTimer);clearTimeout(segmentTimer);
    text=value;expiry=progress===null?Math.min(Number(until)*1000,Date.now()+5000):Infinity;segmentIndex=0;captionKey='';
    caption.classList.toggle('visible',!!text&&expiry>Date.now());
    layout();
    if(Number.isFinite(expiry)&&expiry>Date.now())fadeTimer=setTimeout(()=>{caption.classList.remove('visible');clearTimeout(segmentTimer);},expiry-Date.now());
  }
  function unread(count,latest){
    dot.hidden=!(count>0&&(!latest||latest.id>seenThrough));
    peek.classList.toggle('has-unread',!dot.hidden);
    peek.setAttribute('aria-label',dot.hidden?'消息':'消息，有未读');
  }
  function renderHistory(data){
    list.replaceChildren();
    for(const item of data.messages){
      const row=document.createElement('li');
      const at=new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',hour:'2-digit',minute:'2-digit',hour12:false}).format(item.at*1000);
      row.textContent=`${at} ${item.speaker}：${item.text}`;
      row.classList.toggle('mine',item.speaker==='我');list.append(row);
    }
    historyVersion=data.messages.at(-1)?.id||0;
    seenThrough=Math.max(seenThrough,data.read_through||0);unread(data.unread);
    list.scrollTop=list.scrollHeight;
    if(data.error)result.textContent=data.error;
  }
  async function refreshHistory(){
    if(fetching||panel.hidden)return;fetching=true;
    try{
      const response=await fetch(API+'/api/messages',{cache:'no-store',signal:AbortSignal.timeout(5000)});
      if(!response.ok)throw Error('history unavailable');
      const data=await response.json();if(panel.hidden)return;
      renderHistory(data);
      const receipt=await postJSON('/api/messages/read',{through:historyVersion});
      if(receipt){seenThrough=receipt.read_through;unread(receipt.unread);}
    }catch{result.textContent='消息暂未载入，稍后重试';}
    finally{fetching=false;}
  }
  function open(){window.closeSettings?.();panel.hidden=false;document.body.classList.add('messages-open');layout();refreshHistory();}
  function close(){panel.hidden=true;document.body.classList.remove('messages-open');input.blur();}
  peek.addEventListener('click',open);
  document.querySelector('#messages-open').addEventListener('click',open);
  peek.addEventListener('pointerdown',event=>event.stopPropagation());
  document.querySelector('#message-close').addEventListener('click',close);
  panel.addEventListener('click',event=>{if(event.target===panel||event.target===list||event.target.id==='message-surface')close();});
  panel.addEventListener('pointerdown',event=>{
    event.stopPropagation();swipe=event.target.closest('input,textarea,button')||list.scrollTop>0?null:{x:event.clientX,y:event.clientY};
  });
  panel.addEventListener('pointerup',event=>{
    if(swipe&&event.clientY-swipe.y>70&&Math.abs(event.clientX-swipe.x)<60)close();swipe=null;
  });
  // Touch scrolling can cancel pointerup; reserve only a downward pull at the top.
  let pull=null;
  panel.addEventListener('touchstart',event=>{
    const touch=event.touches[0];
    pull=event.touches.length!==1||event.target.closest('input,textarea,button')||list.scrollTop>0?null:{x:touch.clientX,y:touch.clientY};
  },{passive:true});
  panel.addEventListener('touchmove',event=>{
    if(!pull||event.touches.length!==1)return;
    const touch=event.touches[0],dy=touch.clientY-pull.y,dx=Math.abs(touch.clientX-pull.x);
    if(dy>0&&dy>dx){event.preventDefault();if(dy>70){close();pull=null;}}
  },{passive:false});
  panel.addEventListener('touchend',()=>{pull=null;});
  panel.addEventListener('touchcancel',()=>{pull=null;});
  document.querySelector('#message-form').addEventListener('submit',async event=>{
    event.preventDefault();const value=input.value;
    if(!value.trim()||Array.from(value).length>500){result.textContent='请输入1–500字';return;}
    const send=document.querySelector('#message-send');send.disabled=true;result.textContent='发送中…';
    let data,sent=false;
    try{if(window.faceCall?.active){const reply=await window.faceCall.sendText(value);sent=reply.sent;data=reply.history;}
      else{data=await postJSON('/api/messages',{text:value});sent=!!data;}}catch{data=null;}
    send.disabled=false;
    if(!sent){result.textContent='未送出，文字已保留，请重试';return;}
    if(input.value===value)input.value='';if(data)renderHistory(data);result.textContent=data?'已送出':'已送出，记录稍后显示';
  });
  input.addEventListener('input',()=>{result.textContent=Array.from(input.value).length+'/500';});
  window.addEventListener('keydown',event=>{if(event.key==='Escape'&&!panel.hidden)close();});
  window.addEventListener('resize',layout);window.visualViewport?.addEventListener('resize',layout);
  window.faceMessages={
    layout,geometry:()=>geometry,showSubtitle,open,close,
    showCallSubtitle(value,progress=0){callCaption={text:value,progress:Number.isFinite(progress)?Math.max(0,Math.min(1,progress)):0};showSubtitle(value,0,callCaption.progress);},
    clearCallSubtitle(){callCaption=null;if(lastData)this.update(lastData);},
    update(data){
      lastData=data;
      if((data.system_notice||'')!==noticeText){noticeText=data.system_notice||'';noticeUntil=Date.now()/1000+5;}
      if(noticeText&&noticeUntil>Date.now()/1000)showSubtitle(noticeText,noticeUntil);
      else if(callCaption)showSubtitle(callCaption.text,0,callCaption.progress);
      else showSubtitle(data.subtitle||'',data.subtitle_until||0);
      const latest=data.latest_message;unread(data.unread||0,latest);
      if(!panel.hidden&&data.message_id!==historyVersion)refreshHistory();
      layout();
    },
  };
})();

// Shared work phrase; face.js owns the RAF, eye-expressions owns head/eye drawing.
(() => {
  const ns='http://www.w3.org/2000/svg',simple=document.querySelector('#simple');
  const create=(tag,attributes,parent)=>{
    const node=document.createElementNS(ns,tag);
    for(const [key,value] of Object.entries(attributes))node.setAttribute(key,value);
    parent.append(node);return node;
  };
  const typing=create('g',{'aria-hidden':'true','pointer-events':'none',display:'none',fill:'#9cd4ca'},simple.querySelector('.glasses-scene'));
  // Seen across the desk: spacebar by the typist; QWERTY row nearest the viewer.
  const keys=[];
  ['qwertyuiop','asdfghjkl','zxcvbnm',' '].forEach((letters,row)=>{
    const spacing=[-20,-18.5,-17.5,0][row],size=[17.5,16.25,15,76][row],x0=[261.25,250.5,238.125,142][row],y=[290,273.75,257.875,243][row],depth=[13,12.5,11.25,10][row];
    [...letters].forEach((letter,col)=>{
      const x=x0+col*spacing,node=create('g',{class:'work-block work-key','data-key':letter,'data-row':row,'data-x':x,opacity:0},typing);
      create('path',{d:`M${x-1} ${y+depth} h${size+2} v2.5 h${-size-2} Z`,fill:'#416f68'},node);
      create('path',{d:`M${x+1} ${y} h${size-2} l2 ${depth} h${-size-2} Z`},node);
      keys.push({letter,node});
    });
  });
  // Follow a sample sentence's actual key positions, including word pauses.
  const strokes=[];let typedAt=0;
  for(const letter of 'the quick brown fox'){
    strokes.push({letter,at:typedAt});typedAt+=letter===' '?340:[95,145,115,180,120,150,105][strokes.length%7];
  }
  const typingLead=650,typingCycle=typingLead+typedAt+1700;
  const offsets=[[-2,-1],[0,-2],[2,-1],[2,1],[0,2],[-2,1]];
  let mode=null,modeSince=0,drift=[0,0],lastTick=-1;
  const phase=now=>Math.max(0,now-modeSince)%typingCycle;
  const beats=[[0,0,0,0,0,0],[.06,0,0,0,-.8,2.4],[.15,-1.5,5,-1.4,-1.3,3],[.32,-3,7,-1.8,-.5,3],[.53,2.3,6,1.2,1.4,2.8],[.68,1.2,8,.6,0,2.5],[.78,.5,4,.2,0,0],[.9,0,0,0,0,0],[1,0,0,0,0,0]];
  function motion(now=Date.now()){
    if(mode!=='typing')return null;
    const t=phase(now)/typingCycle,index=beats.findIndex(beat=>beat[0]>t),a=beats[index-1],b=beats[index];
    const fraction=(t-a[0])/(b[0]-a[0]),ease=fraction*fraction*(3-2*fraction);
    return a.slice(1).map((value,i)=>value+(b[i+1]-value)*ease);
  }
  function update({state='waiting',now=Date.now(),closed=false,quiet=false,cooling=false,hidden=false}={}){
    const next=closed||quiet||cooling||hidden?null:state==='working'?'typing':null;
    const changed=next!==mode;
    if(changed){
      mode=next;modeSince=now;typing.setAttribute('display',mode==='typing'?'inline':'none');
    }
    if(!mode)return;
    // Cap decoration writes at 20fps; no extra timer or animation loop.
    const tick=Math.floor(now/50);if(!changed&&tick===lastTick)return;lastTick=tick;
    drift=offsets[Math.floor(now/30000)%offsets.length];
    typing.setAttribute('transform',`translate(${drift.join(' ')})`);
    keys.forEach(({letter,node})=>{
      const age=Math.min(...strokes.filter(hit=>hit.letter===letter).map(hit=>phase(now)-typingLead-hit.at).filter(age=>age>=0),typingCycle);
      const alpha=age<40?.5*age/40:age<140?.5:age<280?.5-.26*(age-140)/140:age<850?.24:age<1500?.24*(1500-age)/650:0;
      const press=age<75?age/75:age<115?1:age<210?1-1.16*(age-115)/95:age<300?-.16*(300-age)/90:0;
      node.setAttribute('opacity',alpha.toFixed(3));node.setAttribute('transform',`translate(0 ${(press*6).toFixed(3)})`);
    });
  }
  window.workIndicator={update,motion,bounds:()=>mode==='typing'?[typing.getBoundingClientRect()]:[],diagnostic:()=>({mode,drift:[...drift],drift_interval_ms:30000,max_opacity:mode==='typing'?.5:0})};
})();

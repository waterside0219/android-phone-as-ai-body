// One contour per eye, sampled once; the existing face RAF owns every morph.
(() => {
  const ns='http://www.w3.org/2000/svg',count=96;
  function contour(d){
    const path=document.createElementNS(ns,'path');path.setAttribute('d',d);
    const length=path.getTotalLength();
    return Array.from({length:count},(_,i)=>{const p=path.getPointAtLength(length*i/count);return [p.x,p.y];});
  }
  const capsule=(x,y,w,h)=>`M${x+w/2} ${y} A${w/2} ${w/2} 0 0 1 ${x+w} ${y+w/2} V${y+h-w/2} A${w/2} ${w/2} 0 0 1 ${x} ${y+h-w/2} V${y+w/2} A${w/2} ${w/2} 0 0 1 ${x+w/2} ${y} Z`;
  const shapes={
    normal:contour(capsule(-11,-27,22,54)),
    blink:contour('M0 -4.5 H20 A4.5 4.5 0 0 1 20 4.5 H-20 A4.5 4.5 0 0 1 -20 -4.5 Z'),
    half:contour('M0 -9 H26 A3 3 0 0 1 26 -3 H17 A17 17 0 0 1 -17 -3 H-26 A3 3 0 0 1 -26 -9 Z'),
    smile:contour('M0 -11.5 Q13 -11.5 23.7 5.5 A4.5 4.5 0 0 1 16.3 10.5 Q0 -15 -16.3 10.5 A4.5 4.5 0 0 1 -23.7 5.5 Q-13 -11.5 0 -11.5 Z'),
    heart:contour('M0 -10 C24 -26 34 0 0 22 C-34 0 -24 -26 0 -10 Z'),
    squint:contour('M0 -6 H12 A6 6 0 0 1 12 6 H-12 A6 6 0 0 1 -12 -6 Z'),
    surprised:contour('M0 -22 A22 22 0 0 1 0 22 A22 22 0 0 1 0 -22 Z'),
    sleepy:contour('M0 -5 H24 A3 3 0 0 1 24 1 H12 A12 9 0 0 1 -12 1 H-24 A3 3 0 0 1 -24 -5 Z'),
    thinking:contour(capsule(4,-36,22,44)),
    working:contour(capsule(-11,-4,22,34)),
    stuck:contour('M0 -5 L11.5 -16.5 A3.5 3.5 0 0 1 16.5 -11.5 L5 0 L16.5 11.5 A3.5 3.5 0 0 1 11.5 16.5 L0 5 L-11.5 16.5 A3.5 3.5 0 0 1 -16.5 11.5 L-5 0 L-16.5 -11.5 A3.5 3.5 0 0 1 -11.5 -16.5 Z'),
    angry:contour('M-15.6 -19.8 L10.4 -3.8 Q16 0 10.4 3.8 L-15.6 19.8 A4.5 4.5 0 0 1 -20.4 12.2 L-.6 0 L-20.4 -12.2 A4.5 4.5 0 0 1 -15.6 -19.8 Z'),
  };
  shapes.cooling=shapes.smile.map(([x,y])=>[x,-y]);
  const pose=(name,frame=[0,0,0])=>({eyes:[shapes[name],shapes[name]],frame,transition:650});
  const presets={
    normal:pose('normal'),blink:pose('blink'),half:pose('half',[0,3,1.3]),
    smile:pose('smile',[0,-6,-1.5]),heart:pose('heart',[0,-7,1.8]),squint:pose('squint',[0,2,0]),
    surprised:pose('surprised',[0,-7,0]),sleepy:pose('sleepy',[0,4.5,-1.6]),
    thinking:pose('thinking',[1.5,-4,1.8]),working:pose('working',[0,3,-1.2]),
    stuck:pose('stuck',[0,1.5,-2]),angry:{...pose('angry',[0,-3,2]),eyes:[shapes.angry,shapes.angry.map(([x,y])=>[-x,y]).reverse()]},
    glint:pose('normal'),
    cooling:{...pose('cooling'),transition:0},
  };
  const sequences={waiting:['normal'],thinking:['thinking','half'],working:['working','squint'],stuck:['stuck','angry'],present:['smile','normal','smile','half']};
  const touchShapes={startled:'heart',hold:'heart',chin:'heart',kiss:'heart',tap:'heart'};
  const simple=document.querySelector('#simple'),rig=simple.querySelector('#glasses-rig');
  let centerKey='',center=null;
  function centerScene(force=false){
    const width=window.visualViewport?.width||innerWidth,height=window.visualViewport?.height||innerHeight,gap=screen.height-height;
    const key=[width,height,screen.width,screen.height].join('|');
    if(!force&&key===centerKey)return;
    centerKey=key;
    // This phone's fullscreen inner PWA starts below the native top cutout.
    // The cover display's narrower app area must keep its own center.
    const inset=matchMedia('(display-mode: fullscreen)').matches&&Math.abs(screen.width-width)<2&&height>width*2&&gap>0&&gap<screen.height*.1?gap:0;
    simple.style.setProperty('--eye-inset-y',(-inset/2)+'px');
    center={width,height,screen_width:screen.width,screen_height:screen.height,inset_y:-inset/2};
  }
  centerScene();window.addEventListener('resize',()=>centerScene(true));window.visualViewport?.addEventListener('resize',()=>centerScene(true));
  const paths=Array.from(simple.querySelectorAll('.eye-shape')),lids=Array.from(simple.querySelectorAll('.eye-blink')),gazes=Array.from(simple.querySelectorAll('.eye-gaze')),glints=Array.from(simple.querySelectorAll('.lens-glint'));
  const svg=(tag,attributes,parent)=>{const node=document.createElementNS(ns,tag);for(const [k,v] of Object.entries(attributes))node.setAttribute(k,v);parent.append(node);return node;};
  const rgb=hex=>hex.replace('#','').match(/../g).map(v=>parseInt(v,16));
  const green=rgb(getComputedStyle(simple).getPropertyValue('--green').trim()),defs=simple.querySelector('defs'),decorations=simple.querySelector('#eye-decorations');
  const outfits=window.eyeOutfits||[{id:'daily',label:'日常'}],textures=[],decor=[];
  for(const outfit of outfits){
    outfit.presets={};
    const atlas=outfit.atlas;
    if(outfit.color)for(const [name,preset] of Object.entries(presets))outfit.presets[name]={...preset,color:rgb(outfit.color)};
    if(atlas){const pattern=svg('pattern',{id:'decor-'+outfit.id,patternUnits:'userSpaceOnUse',width:atlas.width,height:atlas.height},defs);svg('image',{href:atlas.src,width:atlas.width,height:atlas.height},pattern);}
    for(const [variantName,variant] of Object.entries(outfit.variants||{})){
      const key=outfit.id+'-'+variantName;
      (variant.eyes||[]).forEach((eye,i)=>{
        if(!atlas||!eye.origin)return;
        const id='outfit-'+key+'-'+i,pattern=svg('pattern',{id,patternUnits:'userSpaceOnUse',width:atlas.width,height:atlas.height,patternTransform:`scale(${eye.scale}) translate(${-eye.origin[0]} ${-eye.origin[1]})`},defs);
        svg('image',{href:atlas.src,width:atlas.width,height:atlas.height},pattern);
        const path=svg('path',{class:'outfit-texture',fill:`url(#${id})`,opacity:0},lids[i]);
        textures.push({key,i,path});
      });
      for(const item of variant.decorations||[]){
          const group=svg('g',{class:'outfit-decoration','data-kind':item.kind,'data-active':'false',opacity:0},decorations);
          svg('path',{d:item.d,fill:item.fill||(atlas?`url(#decor-${outfit.id})`:variant.color||outfit.color||'#8f989d')},group);
          decor.push({...item,key,group});
      }
      const eyes=variant.eyes?.map(eye=>contour(eye.d));
      for(const [name,target] of Object.entries(outfit.expressions||{}))if(target===variantName&&presets[name])outfit.presets[name]={...presets[name],eyes:eyes||presets[name].eyes,color:variant.color||outfit.color?rgb(variant.color||outfit.color):green,frame:variant.frame||presets[name].frame,texture:key,variant:variantName};
    }
  }
  let saved='daily';try{saved=localStorage.getItem('face-outfit')||saved;}catch{}
  let outfit=outfits.find(item=>item.id===saved)||outfits[0];
  const options=document.querySelector('#outfit-options');
  function selectOutfit(id,remember=true){
    const next=outfits.find(item=>item.id===id);if(!next)return;
    outfit=next;simple.dataset.outfit=id;
    if(remember)try{localStorage.setItem('face-outfit',id);}catch{}
    for(const button of options?.children||[])button.setAttribute('aria-pressed',String(button.dataset.outfit===id));
  }
  if(options)for(const item of outfits){const button=document.createElement('button');button.type='button';button.textContent=item.label;button.dataset.outfit=item.id;button.addEventListener('click',()=>selectOutfit(item.id));options.append(button);}
  selectOutfit(outfit.id,false);
  let key='',index=0,nextAt=0,current='',applied=null,started=0,lastAt=0,progress=1,blinkAmount=1,lastProgress=-1;
  let from=presets.normal.eyes,rendered=from,frameFrom=[0,0,0],frame=[0,0,0];
  let color=green,colorFrom=green,weights={},weightsFrom={},workPose=[0,0,0,0,0];
  const mix=(a,b,t)=>a+(b-a)*t,clamp=n=>Math.max(0,Math.min(1,n));
  function update({state='waiting',present=false,speaking=false,reaction=null,now=Date.now(),blink=false,closed=false,quiet=false,cooling=false,look={x:0,y:0}}){
    // Android can publish the new physical screen metrics after its resize event.
    // The existing RAF notices that change; unchanged geometry performs no writes.
    centerScene();
    const sequenceKey=quiet?'waiting':speaking?'present':state;
    const sequence=sequences[sequenceKey]||sequences.waiting,nextKey=sequenceKey+'|'+(reaction||'');
    if(nextKey!==key){key=nextKey;index=0;nextAt=now+12000+Math.random()*8000;}
    else if(now>=nextAt){index=(index+1)%sequence.length;nextAt=now+12000+Math.random()*8000;}
    const name=cooling?'cooling':quiet?'half':touchShapes[reaction]||(presets[reaction]?reaction:null)||(speaking?'smile':sequence[index%sequence.length]),preset=cooling?presets.cooling:outfit.presets[name]||presets[name];
    const changed=current!==name||applied!==preset;
    if(changed){
      from=rendered;frameFrom=frame;colorFrom=color;weightsFrom={...weights};started=now;current=name;applied=preset;simple.dataset.expression=name;
    }
    progress=preset.transition?clamp((now-started)/preset.transition):1;
    const eased=progress*progress*(3-2*progress),shrink=name==='heart'?1-.2*Math.sin(Math.PI*progress):1;
    const morphing=changed||progress!==lastProgress;lastProgress=progress;
    if(morphing){
    rendered=from.map((eye,i)=>eye.map((p,j)=>p.map((v,k)=>mix(v,preset.eyes[i][j][k],eased)*shrink)));
    frame=frameFrom.map((v,i)=>mix(v,preset.frame[i],eased));
    color=colorFrom.map((v,i)=>mix(v,(preset.color||green)[i],eased));
    for(const {key} of [...textures,...decor])weights[key]=mix(weightsFrom[key]||0,preset.texture===key?1:0,eased);
    }
    const dt=Math.max(0,Math.min(100,now-(lastAt||now)));lastAt=now;
    const taskPose=state==='working'&&!reaction&&!speaking&&!closed&&!quiet&&!cooling?window.workIndicator?.motion(now):null;
    const priorWork=workPose;
    workPose=cooling?[0,0,0,0,0]:workPose.map((value,i)=>mix(value,taskPose?.[i]||0,1-Math.exp(-dt/(taskPose?100:180))));
    if(morphing||workPose.some((value,i)=>Math.abs(value-priorWork[i])>.0001)){
      const sway=Math.sin(progress*Math.PI)*2.2;
      rig.setAttribute('transform',`translate(${(frame[0]+workPose[0]).toFixed(3)} ${(frame[1]-sway+workPose[1]).toFixed(3)}) rotate(${(frame[2]+sway*.4+workPose[2]).toFixed(3)} 180 112)`);
    }
    const closedTarget=(closed||blink)?.045:1;
    blinkAmount=cooling?1:mix(blinkAmount,closedTarget,1-Math.exp(-dt/(closed||blink?26:55)));
    for(let i=0;i<paths.length;i++){
      if(morphing){
        const d=rendered[i].map((p,j)=>(j?'L':'M')+p.map(v=>v.toFixed(3)).join(' ')).join(' ')+' Z';
        paths[i].setAttribute('d',d);paths[i].style.fill=`rgb(${color.map(Math.round).join(' ')})`;
        for(const texture of textures)if(texture.i===i){texture.path.setAttribute('d',d);texture.path.setAttribute('opacity',(weights[texture.key]||0).toFixed(4));}
      }
      lids[i].setAttribute('transform',`scale(1 ${blinkAmount.toFixed(4)})`);
      gazes[i].setAttribute('transform',`translate(${(closed?0:Math.max(-7,Math.min(7,look.x*7+workPose[3]))).toFixed(3)} ${(closed?0:Math.max(-5,Math.min(5,look.y*5+workPose[4]))).toFixed(3)})`);
      const sweep=clamp((now-started)/1600),shine=name==='glint'&&!closed&&!quiet?Math.sin(Math.PI*sweep)*.16:0;
      glints[i].setAttribute('opacity',shine.toFixed(4));
      glints[i].firstElementChild.setAttribute('transform',`translate(${(-90+sweep*180).toFixed(2)} 0)`);
    }
    for(const item of decor){
      const weight=weights[item.key]||0;
      if(morphing){item.group.setAttribute('opacity',weight.toFixed(4));item.group.dataset.active=String(weight>.001);item.group.style.display=weight>.001?'':'none';}
      if(weight<=.001)continue;
      const fly=item.kind==='bat'&&!closed&&!quiet?Math.sin(now/3200+item.position[0]):0;
      item.group.setAttribute('transform',`translate(${item.position[0]+fly*1.2} ${item.position[1]+fly}) scale(${item.scale}) translate(${-item.origin[0]} ${-item.origin[1]})`);
    }
  }
  window.eyeExpressions={presets,sequences,update,selectOutfit,diagnostic:()=>({name:current,outfit:outfit.id,variant:applied?.variant||null,center,work_motion:workPose.map(v=>Number(v.toFixed(3))),next_at:nextAt,transition:applied?.transition,progress:Number(progress.toFixed(3)),points:count,frame:frame.map(v=>Number(v.toFixed(3))),blink:Number(blinkAmount.toFixed(3))})};
})();

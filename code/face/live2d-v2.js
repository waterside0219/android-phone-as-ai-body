// Live2D V2. One frame owner: face.js. No network CDN or extra RAF.
const VERSION='1';  // 换了模型文件就改一下这个数，让浏览器不用旧缓存
const VIEW=2.35, OFFSET=.53;
let frameworkReady;
function framework(){
  if(!frameworkReady)frameworkReady=new Promise((resolve,reject)=>{
    const script=document.createElement('script');
    script.src=new URL('vendor/cubism-framework.js?v=93ebb742bdc64564',import.meta.url);
    script.onload=()=>resolve(window.Cubism);script.onerror=()=>{script.remove();frameworkReady=null;reject(Error('Cubism Framework load failed'));};
    document.head.append(script);
  });
  return frameworkReady;
}
async function resource(name){
  const response=await fetch(new URL('runtime/'+name+'?v='+VERSION,import.meta.url));
  if(!response.ok)throw Error('Live2D resource '+name+': HTTP '+response.status);
  return response.arrayBuffer();
}
const clamp=(v,min,max)=>Math.max(min,Math.min(max,v));
const ease=t=>.5-.5*Math.cos(Math.PI*clamp(t,0,1));
const pulse=t=>t<0?0:t<.5?ease(t/.5):t<1.3?1-ease((t-.5)/.8):0;

export async function loadV2(canvas){
  const C=await framework();
  C.CubismFramework.startUp();if(!C.CubismFramework.isInitialized())C.CubismFramework.initialize();
  const entry=JSON.parse(new TextDecoder().decode(await resource('model.model3.json'))),refs=entry.FileReferences;
  const [mocBytes,motionBytes,physicsBytes,textureBytes]=await Promise.all([
    resource(refs.Moc),resource(refs.Motions.Idle[0].File),resource(refs.Physics),resource(refs.Textures[0])]);
  let moc,model,fm,motion,queue,physics,gl,program,texture,buffers=[],shaders=[],audio=null;
  let speechEvents=null,speech=null,envelope=null,decodeAbort=null;
  let time=0,previous=null,mouth=0,earTouches={L:null,R:null},neutralEars={};
  const api={canvas,get model(){return model;},get drawables(){return model?.drawables;},frame,reset,suspend,dispose,setAudioSource,pokeEar,hitEar,earPoint,exclusion};
  try{
    moc=Live2DCubismCore.Moc.fromArrayBuffer(mocBytes);if(!moc)throw Error('V2 MOC load failed');
    model=Live2DCubismCore.Model.fromMoc(moc);if(!model)throw Error('V2 Core model load failed');
    fm=new C.CubismModel(model);fm.initialize();
    motion=C.CubismMotion.create(motionBytes,motionBytes.byteLength);
    const ids=name=>{const v=new C.csmVector();for(const id of entry.Groups.find(g=>g.Name===name)?.Ids||[])v.pushBack(C.CubismFramework.getIdManager().getId(id));return v;};
    motion.setEffectIds(ids('EyeBlink'),ids('LipSync'));motion.setIsLoop(true);motion.setIsLoopFadeIn(false);
    gl=canvas.getContext('webgl',{alpha:true,antialias:true,preserveDrawingBuffer:true,premultipliedAlpha:true,stencil:true});
    if(!gl)throw Error('WebGL unavailable');
    const shader=(type,source)=>{const s=gl.createShader(type);shaders.push(s);gl.shaderSource(s,source);gl.compileShader(s);if(!gl.getShaderParameter(s,gl.COMPILE_STATUS))throw Error(gl.getShaderInfoLog(s));return s;};
    program=gl.createProgram();
    gl.attachShader(program,shader(gl.VERTEX_SHADER,'attribute vec2 p;attribute vec2 t;uniform vec2 view;varying vec2 uv;void main(){uv=vec2(t.x,1.0-t.y);gl_Position=vec4(p.x*view.x,p.y*view.y-0.53,0.0,1.0);}'));
    gl.attachShader(program,shader(gl.FRAGMENT_SHADER,'precision highp float;uniform sampler2D texture0;uniform float opacity;uniform bool masking;varying vec2 uv;void main(){vec4 c=texture2D(texture0,uv);if(masking&&c.a<0.05)discard;gl_FragColor=vec4(c.rgb,c.a*opacity);}'));
    gl.linkProgram(program);if(!gl.getProgramParameter(program,gl.LINK_STATUS))throw Error(gl.getProgramInfoLog(program));gl.useProgram(program);
    for(const key of ['view','opacity','masking'])program[key]=gl.getUniformLocation(program,key);
    program.position=gl.getAttribLocation(program,'p');program.uv=gl.getAttribLocation(program,'t');
    gl.enableVertexAttribArray(program.position);gl.enableVertexAttribArray(program.uv);
    gl.enable(gl.BLEND);gl.blendFuncSeparate(gl.SRC_ALPHA,gl.ONE_MINUS_SRC_ALPHA,gl.ONE,gl.ONE_MINUS_SRC_ALPHA);
    texture=gl.createTexture();gl.bindTexture(gl.TEXTURE_2D,texture);
    const image=new Image(),url=URL.createObjectURL(new Blob([textureBytes],{type:'image/png'}));
    try{image.src=url;await image.decode();gl.texImage2D(gl.TEXTURE_2D,0,gl.RGBA,gl.RGBA,gl.UNSIGNED_BYTE,image);}finally{URL.revokeObjectURL(url);}
    // At least 2 physical samples per original-art pixel, matching the approved
    // preview. NEAREST is used only with this oversampling and WebGL MSAA.
    gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MIN_FILTER,gl.NEAREST);gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MAG_FILTER,gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_WRAP_S,gl.CLAMP_TO_EDGE);gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_WRAP_T,gl.CLAMP_TO_EDGE);
    const d=model.drawables;
    buffers=Array.from({length:d.count},(_,i)=>{
      const b={position:gl.createBuffer(),uv:gl.createBuffer(),index:gl.createBuffer()};
      gl.bindBuffer(gl.ARRAY_BUFFER,b.uv);gl.bufferData(gl.ARRAY_BUFFER,d.vertexUvs[i],gl.STATIC_DRAW);
      gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER,b.index);gl.bufferData(gl.ELEMENT_ARRAY_BUFFER,d.indices[i],gl.STATIC_DRAW);return b;
    });
    reset();model.update();for(const side of ['L','R'])neutralEars[side]=meshBounds(new RegExp('^Ear'+side+'$'));
    draw();return api;
  }catch(error){dispose();throw error;}

  function set(id,value){const p=model.parameters,i=p.ids.indexOf(id);if(i>=0)p.values[i]=clamp(value,p.minimumValues[i],p.maximumValues[i]);}
  function silence(){mouth=0;for(const id of ['ParamMouthOpenY','ParamMouthForm','ParamEyeLSmile','ParamEyeRSmile'])set(id,0);}
  function setAudioSource(source){
    if(audio?.source===source)return;
    if(audio){audio.source.removeEventListener?.('ended',ended);try{audio.source.disconnect(audio.analyser);}catch{}audio.analyser.disconnect();audio=null;}
    if(model)silence();
    if(source){const analyser=source.context.createAnalyser();analyser.fftSize=1024;source.connect(analyser);audio={source,analyser,samples:new Float32Array(analyser.fftSize)};source.addEventListener?.('ended',ended);}
  }
  function ended(){setAudioSource(null);}
  function stopSpeech(){
    speechEvents?.close();speechEvents=null;decodeAbort?.abort();decodeAbort=null;speech=null;envelope=null;
    if(model)silence();
  }
  function receiveSpeech(){
    if(speechEvents)return;
    speechEvents=new EventSource('http://127.0.0.1:8192/api/speech-events');
    speechEvents.onerror=()=>{speech=null;envelope=null;decodeAbort?.abort();if(model)silence();};
    speechEvents.onmessage=async event=>{
      const next=JSON.parse(event.data),changed=next?.id!==speech?.id;speech=next;
      if(!next){decodeAbort?.abort();envelope=null;if(model)silence();return;}
      if(!changed)return;
      envelope=null;decodeAbort?.abort();const controller=decodeAbort=new AbortController();
      try{
        const response=await fetch('http://127.0.0.1:8192/api/speech-audio/'+encodeURIComponent(next.id),{cache:'no-store',signal:controller.signal});
        if(!response.ok)throw Error('Speech audio unavailable');
        const context=new OfflineAudioContext(1,1,16000),buffer=await context.decodeAudioData(await response.arrayBuffer());
        const count=Math.ceil(buffer.duration/.02),values=new Float32Array(count),width=Math.max(1,Math.round(buffer.sampleRate*.02));
        for(let channel=0;channel<buffer.numberOfChannels;channel++){
          const pcm=buffer.getChannelData(channel);
          for(let k=0;k<count;k++){let sum=0,start=k*width,end=Math.min(pcm.length,start+width);for(let i=start;i<end;i++)sum+=pcm[i]*pcm[i];values[k]+=sum/Math.max(1,end-start)/buffer.numberOfChannels;}
        }
        for(let i=0;i<count;i++)values[i]=Math.sqrt(values[i]);
        if(!controller.signal.aborted&&speech?.id===next.id)envelope={values,duration:buffer.duration};
      }catch{if(speech?.id===next.id){envelope=null;if(model)silence();}}
    };
  }
  function reset(){
    if(!model)return;
    setAudioSource(null);model.parameters.values.set(model.parameters.defaultValues);
    earTouches={L:null,R:null};time=0;previous=null;
    queue?.release();queue=new C.CubismMotionQueueManager();queue.startMotion(motion,false);queue.doUpdateMotion(fm,0);
    physics?.release();physics=C.CubismPhysics.create(physicsBytes,physicsBytes.byteLength);physics.stabilization(fm);
  }
  function suspend(){stopSpeech();reset();if(model)model.update();}
  function pokeEar(side){if(side==='L'||side==='R')earTouches[side]=0;}
  function frame(now,{look=null,reaction=null,closed=false,blink=null,muted=false}={}){
    if(!model)return;
    if(previous!==null&&now-previous>1000)reset();
    const dt=previous===null?0:clamp((now-previous)/1000,0,.1);previous=now;time+=dt;
    model.parameters.values.set(model.parameters.defaultValues);queue.doUpdateMotion(fm,time);
    if(look){set('ParamEyeBallX',look.x);set('ParamEyeBallY',-look.y);set('ParamAngleX',look.x*(reaction==='chin'?12:8));set('ParamAngleY',-look.y*4+(reaction==='chin'?3:0));set('ParamAngleZ',reaction==='hold'||reaction==='chin'?look.x*4:0);}
    if(closed||reaction==='hold'||reaction==='chin'||blink!==null){const eye=closed||reaction==='hold'?0:reaction==='chin'?.16:blink;set('ParamEyeLOpen',eye);set('ParamEyeROpen',eye);}
    for(const side of ['L','R'])if(earTouches[side]!==null){earTouches[side]+=dt;set('ParamEarTouch'+side,(side==='L'?-1:1)*pulse(earTouches[side]));if(earTouches[side]>=4)earTouches[side]=null;}
    const callSource=window.faceCall?.audioSource;
    if(callSource&&audio?.source!==callSource)setAudioSource(callSource);
    receiveSpeech();
    let rms=0,playing=false;
    if(audio?.source.context.state==='running'){audio.analyser.getFloatTimeDomainData(audio.samples);for(const v of audio.samples)rms+=v*v;rms=Math.sqrt(rms/audio.samples.length);playing=true;}
    else if(!audio&&speech?.state==='playing'&&envelope){
      const position=speech.position+Math.max(0,Date.now()/1000-speech.clock_at);
      if(position<envelope.duration){rms=envelope.values[Math.floor(position/.02)]||0;playing=true;}
    }
    if(!playing||muted||closed)silence();
    else{const target=Math.min(.85,Math.max(0,(rms-.012)*4.5));mouth+=(target-mouth)*(1-Math.exp(-dt/(target>mouth?.035:.075)));set('ParamMouthOpenY',mouth);set('ParamMouthForm',.4*mouth/.85);set('ParamEyeLSmile',.08*mouth/.85);set('ParamEyeRSmile',.08*mouth/.85);}
    physics.evaluate(fm,dt);model.update();draw();
  }
  function renderMesh(i){const d=model.drawables,b=buffers[i];gl.bindBuffer(gl.ARRAY_BUFFER,b.position);gl.bufferData(gl.ARRAY_BUFFER,d.vertexPositions[i],gl.DYNAMIC_DRAW);gl.vertexAttribPointer(program.position,2,gl.FLOAT,false,0,0);gl.bindBuffer(gl.ARRAY_BUFFER,b.uv);gl.vertexAttribPointer(program.uv,2,gl.FLOAT,false,0,0);gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER,b.index);gl.uniform1f(program.opacity,d.opacities[i]);gl.drawElements(gl.TRIANGLES,d.indexCounts[i],gl.UNSIGNED_SHORT,0);}
  function draw(){
    const bounds=canvas.getBoundingClientRect();if(!bounds.width||!bounds.height)return;
    const height=Math.max(Math.ceil(4000/VIEW),Math.round(bounds.height*devicePixelRatio)),width=Math.round(height*bounds.width/bounds.height);
    if(canvas.width!==width||canvas.height!==height){canvas.width=width;canvas.height=height;gl.viewport(0,0,width,height);}
    gl.uniform2f(program.view,VIEW*height/width,VIEW);gl.clearColor(0,0,0,0);gl.clear(gl.COLOR_BUFFER_BIT|gl.STENCIL_BUFFER_BIT);
    const d=model.drawables,order=Array.from({length:d.count},(_,i)=>i).sort((a,b)=>d.renderOrders[a]-d.renderOrders[b]);
    for(const i of order){if(d.opacities[i]<=0)continue;
      if(d.masks[i].length){gl.enable(gl.STENCIL_TEST);gl.stencilMask(255);gl.clear(gl.STENCIL_BUFFER_BIT);gl.stencilFunc(gl.ALWAYS,1,255);gl.stencilOp(gl.KEEP,gl.KEEP,gl.REPLACE);gl.colorMask(false,false,false,false);gl.uniform1i(program.masking,1);for(const mask of d.masks[i])renderMesh(mask);gl.colorMask(true,true,true,true);gl.uniform1i(program.masking,0);gl.stencilFunc(gl.EQUAL,1,255);gl.stencilOp(gl.KEEP,gl.KEEP,gl.KEEP);}
      renderMesh(i);gl.disable(gl.STENCIL_TEST);
    }
  }
  function meshBounds(pattern){
    const d=model.drawables;let left=Infinity,top=Infinity,right=-Infinity,bottom=-Infinity;
    for(let i=0;i<d.count;i++)if(pattern.test(d.ids[i])){const v=d.vertexPositions[i];for(let j=0;j<v.length;j+=2){const x=(v[j]+.48)*1000,y=(.64-v[j+1])*1000;left=Math.min(left,x);right=Math.max(right,x);top=Math.min(top,y);bottom=Math.max(bottom,y);}}
    return {left,top,right,bottom};
  }
  function project(x,y){const b=canvas.getBoundingClientRect(),scale=VIEW*b.height/2000;return {x:b.left+b.width/2+(x-480)*scale,y:b.top+(1+OFFSET)*b.height/2+(y-640)*scale};}
  function earRect(side){const now=meshBounds(new RegExp('^Ear'+side+'$')),base=neutralEars[side],dx=(now.left+now.right-base.left-base.right)/2,dy=now.bottom-base.bottom;const a=project((side==='L'?535:270)+dx,dy),b=project((side==='L'?675:410)+dx,210+dy);return {left:a.x,top:a.y,right:b.x,bottom:b.y};}
  function earPoint(side){const r=earRect(side);return {x:(r.left+r.right)/2,y:(r.top+r.bottom)/2};}
  function hitEar(x,y){if(!model)return null;const b=canvas.getBoundingClientRect();if(x<b.left||x>b.right||y<b.top||y>b.bottom)return null;for(const s of ['L','R']){const r=earRect(s);if(x>=r.left&&x<=r.right&&y>=r.top&&y<=r.bottom)return s;}return null;}
  function exclusion(){if(!model)return null;const b=meshBounds(/^(FaceBase|Glasses|Ear[LR]|Brow|Eye|Iris|Highlight|Lid|Lip|Mouth)/),a=project(b.left,b.top),z=project(b.right,b.bottom);return {left:a.x-10,top:a.y-10,right:z.x+10,bottom:z.y+10};}
  function dispose(){
    stopSpeech();if(model)setAudioSource(null);queue?.release();physics?.release();motion?.release();
    if(fm)fm.release();else model?.release();moc?._release();
    if(gl){for(const b of buffers)for(const v of Object.values(b))gl.deleteBuffer(v);if(texture)gl.deleteTexture(texture);if(program)gl.deleteProgram(program);for(const s of shaders)gl.deleteShader(s);}
    model=null;fm=null;moc=null;queue=null;physics=null;motion=null;buffers=[];shaders=[];
  }
}

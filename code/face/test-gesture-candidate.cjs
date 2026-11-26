// F1 v2: synthetic pipeline tests, not real-hand accuracy evidence.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const {test}=require('node:test');
const source=fs.readFileSync(__dirname+'/vision-worker.js','utf8');
function hand(x=.5,y=.7){return Array.from({length:21},(_,i)=>({x:x+(i%4)*.02,y:y-i*.005,z:0}));}
function worker(){
 const outputs=[],c=vm.createContext({postMessage:m=>outputs.push(m)});vm.runInContext(source,c);
 c.points=[hand()];
 vm.runInContext(`faceLandmarker={detectForVideo:()=>({faceLandmarks:[],facialTransformationMatrixes:[]})};gestureRecognizer={recognizeForVideo:()=>({landmarks:points,gestures:points.map(()=>[{categoryName:'None',score:.01}])})};personDetector={detectForVideo:()=>({detections:[]})}`,c);
 return {c,outputs,async frame(t,points=c.points,allowed=true){c.points=points;c.t=t;c.allowed=allowed;await vm.runInContext('pendingFrame={timestamp:t,bitmap:{close(){}},candidate_allowed:allowed};processFrames()',c);return outputs.at(-1);},reply(candidate,gesture='none'){c.reply={type:'gesture_result',candidate_id:candidate.id,gesture};vm.runInContext('onmessage({data:reply})',c);}};
}
test('low-confidence landmarks below chin become a candidate after 600ms, never a local gesture',async()=>{
 const w=worker();for(const t of [1000,1200,1400])assert.equal((await w.frame(t)).gesture_candidate,null);
 const m=await w.frame(1600);assert.ok(m.gesture_candidate);assert.equal(m.gesture,null);
});
test('same pose repeats at 4s; two none replies suppress until shape changes or hand returns',async()=>{
 const w=worker();let first;
 for(let t=1000;t<=1600;t+=200)first=(await w.frame(t)).gesture_candidate;
 assert.ok(first);w.reply(first);let second;
 for(let t=1800;t<=5600;t+=200){const m=await w.frame(t);if(t<5600)assert.equal(m.gesture_candidate,null);else second=m.gesture_candidate;}
 assert.ok(second);w.reply(second);
 for(let t=5800;t<20000;t+=200)assert.equal((await w.frame(t)).gesture_candidate,null);
 const changed=hand();changed[8].y-=.2;
 for(const t of [20000,20200,20400])assert.equal((await w.frame(t,[changed])).gesture_candidate,null);
 assert.ok((await w.frame(20600,[changed])).gesture_candidate);
 await w.frame(20800,[]);
 for(let t=24000;t<=24600;t+=200)second=(await w.frame(t,[hand()])).gesture_candidate;
 assert.ok(second);
});
test('movement and detector gaps reset 600ms hold; disallowed frame cannot emit',async()=>{
 const w=worker();for(let t=1000;t<2200;t+=200)assert.equal((await w.frame(t,[hand(t/10000)])).gesture_candidate,null);
 assert.equal((await w.frame(9000)).gesture_candidate,null);
 for(const t of [9200,9400,9600])assert.equal((await w.frame(t,w.c.points,false)).gesture_candidate,null);
});
test('rate limits cap six/minute and sixty/hour, including held recognized poses',async()=>{
 const w=worker();let count=0;
 for(let t=1000;t<601000;t+=200){const m=await w.frame(t);if(m.gesture_candidate){count++;w.reply(m.gesture_candidate,'wave');}}
 assert.equal(count,60);
 for(let t=601000;t<660000;t+=200)assert.equal((await w.frame(t)).gesture_candidate,null);
 const times=w.outputs.filter(m=>m.gesture_candidate).map(m=>m.gesture_candidate.at);
 assert.ok(times.every((t,i)=>i===0||t-times[i-1]>=4000));
 assert.ok(times.every(t=>times.filter(x=>x>t-60000&&x<=t).length<=6));
});

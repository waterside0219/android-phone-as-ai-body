const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const context=vm.createContext({});
vm.runInContext(fs.readFileSync(__dirname+'/vision-worker.js','utf8'),context);
function hand(x){const points=Array.from({length:21},()=>({x,y:.55,z:0}));points[0].y=.8;points[9].y=.6;for(const i of [4,8,12,16,20])points[i].y=.3;points[4].x=x-.3;return points;}
function sequence(category,xs){vm.runInContext('reset()',context);return xs.map((x,i)=>{context.h={landmarks:[hand(x)]};context.r={gestures:[[{categoryName:category,score:.95}]]};context.t=i*200;return vm.runInContext('detectGesture(h,r,t)',context);});}
assert.deepEqual(sequence('Open_Palm',[.45,.5,.55,.5,.45]),[null,null,null,null,'wave'],'five confident waving frames must respond within one second');
assert.deepEqual(sequence('Open_Palm',[.5,.5,.5,.5,.5]),[null,null,null,null,'wave'],'raised Open_Palm greeting uses the documented five-frame classifier gate');
assert.deepEqual(sequence('Victory',[.5,.5,.5,.5,.5]),[null,null,null,null,'victory']);
vm.runInContext('reset()',context);
context.h={landmarks:[hand(.2),hand(.7)]};context.r={gestures:[[{categoryName:'None',score:.99}],[{categoryName:'Open_Palm',score:.95}]]};
let secondHand;for(let i=0;i<5;i++){context.t=i*200;secondHand=vm.runInContext('detectGesture(h,r,t)',context);}
assert.equal(secondHand,'wave','a raised greeting hand need not be the first tracked hand');
console.log('gesture timing and stability passed');
(async()=>{
  const outputs=[];context.postMessage=message=>outputs.push(message);
  context.recognized={landmarks:[hand(.5)],gestures:[[{categoryName:'Victory',score:.95}]],handedness:[[{score:.99}]]};
  vm.runInContext(`reset();faceLandmarker={detectForVideo:()=>({faceLandmarks:[],facialTransformationMatrixes:[]})};handLandmarker={detectForVideo:()=>({landmarks:[]})};gestureRecognizer={recognizeForVideo:()=>recognized}`,context);
  for(let i=1;i<=5;i++){context.frame={timestamp:i*200,bitmap:{close(){}}};await vm.runInContext('pendingFrame=frame;processFrames()',context);}
  assert.equal(outputs.at(-1).local_hint,'victory','classification and raised-hand gate must use the same tracked hand, not a second detector');
  assert.deepEqual(sequence('Thumb_Up',[.5,.5,.5,.5,.5]),[null,null,null,null,'thumb_up'],'relay gesture name contract');
  console.log('same-hand optional hint classification passed');
})().catch(error=>{console.error(error);process.exitCode=1});
function foldedHand(){
 const p=Array.from({length:21},()=>({x:.5,y:.65,z:0}));
 p[0]={x:.5,y:.8,z:0};p[9]={x:.5,y:.6,z:0};
 p[5]={x:.46,y:.6,z:0};p[6]={x:.45,y:.52,z:0};p[7]={x:.46,y:.6,z:0};p[8]={x:.46,y:.68,z:0};
 p[2]={x:.38,y:.7,z:0};p[3]={x:.4,y:.64,z:0};p[4]={x:.45,y:.66,z:0};
 for(const [tip,pip] of [[12,10],[16,14],[20,18]]){p[tip].y=.7;p[pip].y=.55;}
 return p;
}
context.fist=foldedHand();
assert.equal(vm.runInContext('isHeart(fist)',context),false,'curled fist/pinched idle fingers are not a finger heart');
const heart=foldedHand();heart[6]={x:.45,y:.48,z:0};heart[7]={x:.48,y:.44,z:0};heart[8]={x:.51,y:.46,z:0};heart[3]={x:.44,y:.52,z:0};heart[4]={x:.49,y:.44,z:0};
context.heart=heart;assert.equal(vm.runInContext('isHeart(heart)',context),true,'thumb/index crossing above knuckles with other fingers folded');

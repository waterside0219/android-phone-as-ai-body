// Synthetic anatomical gates are regressions, not labelled real-world accuracy evidence.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const {test}=require('node:test');
const source=fs.readFileSync(__dirname+'/vision-worker.js','utf8');
function hand(){
 const p=Array.from({length:21},()=>({x:.5,y:.65,z:0}));
 p[0]={x:.5,y:.8,z:0};p[9]={x:.5,y:.6,z:0};p[17]={x:.6,y:.63,z:0};
 p[5]={x:.46,y:.6,z:0};p[6]={x:.45,y:.48,z:0};p[7]={x:.48,y:.44,z:0};p[8]={x:.51,y:.46,z:0};
 p[2]={x:.38,y:.7,z:0};p[3]={x:.44,y:.52,z:0};p[4]={x:.49,y:.44,z:0};
 for(const [tip,pip]of [[12,10],[16,14],[20,18]]){p[tip].y=.7;p[pip].y=.55;}
 return p;
}
function run(times,make=()=>hand(),chin=.7){
 const c=vm.createContext({});vm.runInContext(source,c);vm.runInContext('reset()',c);
 return times.map((t,i)=>{c.h={landmarks:[make(i)]};c.r={gestures:[[{categoryName:'None',score:.99}]],handedness:[[{categoryName:'Left'}]]};c.t=t;c.chin=chin;
 // Current code has no chin gate; assignment also works before the fix to expose it.
 vm.runInContext('lastChin=chin;lastFaceAt=t',c);return vm.runInContext('detectGesture(h,r,t)',c);});
}
test('heart takes at least 800ms even at high frame rate',()=>assert.deepEqual(run([0,30,60,90,120]),[null,null,null,null,null]));
test('elapsed time, not five frames, opens the gate',()=>assert.deepEqual(run([0,400,800]),[null,null,'heart']));
test('held heart latches until release',()=>assert.deepEqual(run([0,200,400,600,800,1000]),[null,null,null,null,'heart',null]));
test('hand below chin is rejected',()=>assert.equal(run([0,200,400,600,800],()=>hand(),.4).every(v=>v===null),true));
test('missing face/chin cannot certify hand height',()=>assert.equal(run([0,200,400,600,800],()=>hand(),null).every(v=>v===null),true));
test('nearby parallel fingertips do not form a crossing',()=>assert.equal(run([0,200,400,600,800],()=>{const p=hand();p[3]={x:.48,y:.52,z:0};p[4]={x:.48,y:.45,z:0};p[7]={x:.51,y:.52,z:0};p[8]={x:.51,y:.45,z:0};return p;}).every(v=>v===null),true));
test('edge-on hand is rejected',()=>assert.equal(run([0,200,400,600,800],()=>hand().map(p=>({...p,z:(p.x-.5)*3}))).every(v=>v===null),true));
test('large motion resets the hold',()=>assert.equal(run([0,200,400,600,800],i=>hand().map(p=>({...p,x:p.x+i*.07}))).every(v=>v===null),true));
test('long detector gaps cannot prove a continuous hold',()=>assert.equal(run([0,3000,6000,9000,12000]).every(v=>v===null),true));
test('unfolded fingers fail geometry',()=>{const c=vm.createContext({});vm.runInContext(source,c);c.p=hand();c.p[12].y=.35;assert.equal(vm.runInContext('isHeart(p)',c),false);});
// 这一条要一份真人举手挥手的录像数据（每帧的手部关键点），我们的是自己录的，没放进仓库。
const PALM=process.env.PALM_REPLAY||__dirname+'/../.private/palm-replay-landmarks.json';
test('real raised-palm recording still greets',{skip:!fs.existsSync(PALM)&&'set PALM_REPLAY to your own recording'},()=>{
 const c=vm.createContext({});vm.runInContext(source,c);const frames=JSON.parse(fs.readFileSync(PALM));
 const output=frames.map((f,i)=>{c.h={landmarks:f.hands};c.r={gestures:f.categories};c.t=i*200;return vm.runInContext('detectGesture(h,r,t)',c);});assert.equal(output.at(-1),'wave');
});
test('real Worker path supplies current chin height to the gesture gate',async()=>{
 const messages=[],c=vm.createContext({postMessage:m=>messages.push(m)});vm.runInContext(source,c);
 c.points=hand();c.face=Array.from({length:468},()=>({x:.5,y:.4,z:0}));c.face[152].y=.7;
 vm.runInContext(`faceLandmarker={detectForVideo:()=>({faceLandmarks:[face],facialTransformationMatrixes:[]})};gestureRecognizer={recognizeForVideo:()=>({landmarks:[points],gestures:[[{categoryName:'None',score:.99}]],handedness:[[{categoryName:'Left'}]]})}`,c);
 for(const t of [1000,1400,1800]){c.t=t;await vm.runInContext('pendingFrame={timestamp:t,bitmap:{close(){}}};processFrames()',c);}
 assert.deepEqual(messages.map(m=>m.local_hint),[null,null,'heart']);
});

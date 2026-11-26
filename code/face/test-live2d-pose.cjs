const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const c=vm.createContext({});
try{vm.runInContext(fs.readFileSync(__dirname+'/live2d-pose.js','utf8'),c);}catch(e){if(e.code!=='ENOENT')throw e;}
function pose(overrides={}){c.input={now:200,look:{x:.6,y:.1},reaction:null,closed:false,blink:1,pokeAt:0,speaking:false,...overrides};return vm.runInContext('live2dPose(input)',c);}
assert.equal(pose().ParamMouthOpenY,0,'subtitle without playback must keep mouth shut');
assert.ok(pose({speaking:true}).ParamMouthOpenY>0,'confirmed playback moves mouth');
assert.equal(pose({reaction:'hold'}).ParamEyeLOpen,0);
assert.ok(pose({reaction:'hold'}).ParamEarL<-.5,'hold presses ears down');
assert.ok(pose({reaction:'chin'}).ParamEyeLOpen<.3);
assert.ok(pose({reaction:'chin'}).ParamAngleX>0,'chin follows hand direction');
assert.notEqual(pose().ParamEarL,pose({now:1300}).ParamEarL,'poke decays');
assert.notEqual(pose().ParamHairSwing,0,'hair follows touch');
console.log('live2d touch and actual-audio pose passed');

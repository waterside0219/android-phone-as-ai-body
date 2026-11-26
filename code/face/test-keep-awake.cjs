const {readFileSync}=require('node:fs');
const vm=require('node:vm');
const assert=require('node:assert/strict');
(async()=>{
  const callbacks={},locks=[];
  const document={visibilityState:'visible',addEventListener:(name,fn)=>callbacks[name]=fn};
  const navigator={wakeLock:{request:async type=>{assert.equal(type,'screen');const lock={released:false};locks.push(lock);return lock;}}};
  vm.runInNewContext(readFileSync(__dirname+'/keep-awake.js','utf8'),{document,navigator});
  await new Promise(setImmediate);assert.equal(locks.length,1);
  callbacks.visibilitychange();await new Promise(setImmediate);assert.equal(locks.length,1);
  document.visibilityState='hidden';locks[0].released=true;callbacks.visibilitychange();await new Promise(setImmediate);assert.equal(locks.length,1);
  document.visibilityState='visible';callbacks.visibilitychange();await new Promise(setImmediate);assert.equal(locks.length,2);
  console.log('screen wake lock: visible, no duplicate, hidden, resume passed');
})().catch(e=>{console.error(e);process.exitCode=1});

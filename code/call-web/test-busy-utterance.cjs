// 连着说话时服务器回 400"上一句仍在处理中"，网页把它当致命错误整通挂断。
const assert=require('node:assert/strict');
const {client}=require('./test-playback-start.cjs');
(async()=>{
  for(const failing of ['utterance_start']){
    const c=await client();try{
      let failed=false;
      c.model.transport=async message=>{if(message.type===failing&&!failed){failed=true;throw Object.assign(Error('上一句仍在处理中'),{status:400});}};
      c.capture(new Float32Array(48000).fill(.2));await c.flush();await c.advance(1000);await c.flush();
      assert.ok(failed,'busy rejection happened for '+failing);
      assert.equal(c.api.active,true,'busy '+failing+' must not hang up the call');
    }finally{await c.hangup();}
  }
  console.log('busy-utterance ok');
})().catch(e=>{console.error('FAIL',e.message);process.exit(1);});

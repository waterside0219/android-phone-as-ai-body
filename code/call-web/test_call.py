import asyncio
import base64
import json
import unittest
from unittest.mock import patch
from uuid import uuid4
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import server


class CallTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.received=[];self.ends=[];self.ws=None
        self.creation_started=asyncio.Event();self.creation_gate=asyncio.Event();self.creation_gate.set()
        async def create(request):
            self.creation_started.set();await self.creation_gate.wait()
            return web.json_response(await request.json())
        async def end(request):
            self.ends.append(request.match_info['id']);return web.json_response({'ok':True})
        async def stream(request):
            self.ws=web.WebSocketResponse();await self.ws.prepare(request)
            await self.ws.send_json({'type':'ready'})
            async for message in self.ws:
                self.received.append(json.loads(message.data))
            return self.ws
        gateway=web.Application();gateway.router.add_post('/calls',create);gateway.router.add_post('/calls/{id}/end',end);gateway.router.add_get('/calls/{id}/stream',stream)
        self.gateway=TestServer(gateway);await self.gateway.start_server()
        config={'gateway':str(self.gateway.make_url('')),'max_audio_seconds':30}
        self.app=server.create_app(config,'test-only-token');self.client=TestClient(TestServer(self.app));await self.client.start_server()
        self.origin={'Origin':'http://127.0.0.1:8190'}

    async def asyncTearDown(self):
        self.creation_gate.set();await self.client.close();await self.gateway.close()

    async def dial(self):
        identifier=str(uuid4())
        response=await self.client.post('/api/calls',json={'id':identifier},headers=self.origin)
        self.assertEqual(response.status,200)
        self.assertEqual((await response.json())['id'],identifier)
        return identifier

    async def events(self,identifier,after=0):
        response=await self.client.get(f'/api/calls/{identifier}/events?after={after}',headers=self.origin)
        self.assertEqual(response.status,200)
        return await response.json()

    async def send(self,identifier,message):
        response=await self.client.post(f'/api/calls/{identifier}/events',json=message,headers=self.origin)
        self.assertEqual(response.status,200,await response.text())

    async def test_streaming_local_asr_echo_and_long_poll(self):
        identifier=await self.dial();events=await self.events(identifier)
        self.assertTrue(any(item['data']['type']=='ready' for item in events['events']))
        pcm=base64.b64encode(b'\x01\x01'*1600).decode()
        async def utterance(name):
            await self.send(identifier,{'type':'utterance_start','id':name})
            for _ in range(3):await self.send(identifier,{'type':'utterance_audio','id':name,'pcm':pcm})
            self.assertTrue(any(item['type']=='audio' for item in self.received))
            await self.send(identifier,{'type':'utterance_end','id':name,'discard':False})
        with patch.object(server,'transcribe_pcm',return_value='测试一句'):
            await utterance('first')
            for _ in range(10):
                rows=await self.events(identifier,events['cursor']);events=rows
                if any(row['data']['type']=='transcript' for row in rows['events']):break
            self.assertTrue(any(item['type']=='submit' and item['text']=='测试一句' for item in self.received))
        await self.ws.send_json({'type':'line','line':{'role':'assistant','id':'reply','text':'这是一条回声测试'}})
        rows=await self.events(identifier,events['cursor'])
        with patch.object(server,'transcribe_pcm',return_value='这是一条回声测试'):
            await utterance('echo')
            for _ in range(10):
                rows=await self.events(identifier,rows['cursor'])
                if any(row['data']['type']=='echo_dropped' for row in rows['events']):break
            self.assertEqual(sum(item['type']=='submit' for item in self.received),1)

    async def test_cancel_during_gateway_create_cleans_late_room(self):
        identifier=str(uuid4());self.creation_gate.clear()
        pending=asyncio.create_task(self.client.post('/api/calls',json={'id':identifier},headers=self.origin))
        await self.creation_started.wait()
        response=await self.client.post(f'/api/calls/{identifier}/end',headers=self.origin)
        self.assertEqual(response.status,200)
        self.creation_gate.set();response=await pending
        self.assertEqual(response.status,410)
        self.assertIn(identifier,self.ends)

    async def test_cancel_while_previous_call_closes_keeps_tombstone(self):
        old=await self.dial();room=self.app['rooms'][old]
        entered=asyncio.Event();release=asyncio.Event();original=room.close
        async def close(*args,**kwargs):
            entered.set();await release.wait();return await original(*args,**kwargs)
        identifier=str(uuid4())
        with patch.object(room,'close',side_effect=close):
            pending=asyncio.create_task(self.client.post('/api/calls',json={'id':identifier},headers=self.origin))
            await entered.wait()
            await self.client.post(f'/api/calls/{identifier}/end',headers=self.origin)
            release.set();response=await pending
        self.assertEqual(response.status,410)
        self.assertTrue(self.app['rooms'][identifier].closed)

    async def test_other_websites_rejected_before_dial(self):
        response=await self.client.post('/api/calls',json={'id':str(uuid4())},headers={'Origin':'https://untrusted.example'})
        self.assertEqual(response.status,403)
        self.assertFalse(self.creation_started.is_set())

    async def test_incoming_is_native_only_idempotent_and_accepts_existing_call(self):
        identifier=str(uuid4())
        response=await self.client.post('/api/incoming',json={'id':identifier},headers=self.origin)
        self.assertEqual(response.status,403)
        response=await self.client.post('/api/incoming',json={'id':identifier})
        first=(await response.json())['incoming']
        response=await self.client.post('/api/incoming',json={'id':identifier})
        self.assertEqual(response.status,200)
        self.assertEqual((await response.json())['incoming'],first)
        response=await self.client.post('/api/calls',json={'id':identifier},headers=self.origin)
        self.assertEqual(response.status,200)
        self.assertFalse(self.creation_started.is_set(),'incoming uses the existing gateway call')
        self.assertIsNone(self.app['incoming'])
        await self.client.post(f'/api/calls/{identifier}/end',headers=self.origin)
        response=await self.client.post('/api/incoming',json={'id':identifier})
        self.assertEqual(response.status,410,'a duplicate must not ring a rejected call again')

    async def test_incoming_expiry_saves_missed_line_once(self):
        identifier=str(uuid4());await self.client.post('/api/incoming',json={'id':identifier})
        self.app['incoming']['expires']=0
        saved=[];original=self.app['session'].post
        class Reply:
            status=200
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            async def read(self):return b'{}'
        def post(url,**kwargs):
            if url=='http://127.0.0.1:8192/api/call-line':saved.append(kwargs['json']);return Reply()
            return original(url,**kwargs)
        with patch.object(self.app['session'],'post',side_effect=post):
            for _ in range(50):
                await asyncio.sleep(.05)
                if saved:break
        self.assertEqual(len(saved),1)
        self.assertEqual(saved[0]['text'],'未接来电')
        self.assertIsNone(self.app['incoming'])
        self.assertIn(identifier,self.ends)

    async def test_end_retry_never_claims_unconfirmed_success(self):
        room=server.CallRoom(self.app,str(uuid4()))
        from unittest.mock import AsyncMock
        room.notify_end=AsyncMock(side_effect=[False,False,True])
        self.assertFalse(await room.close())
        self.assertFalse(await room.close())
        self.assertTrue(await room.close())
        self.assertEqual(room.notify_end.await_count,3)

    def test_cancel_stops_the_real_transcription_process(self):
        import tempfile,threading,time,sys,os
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);pidfile=root/'pid';binary=root/'fake-whisper'
            binary.write_text('#!'+sys.executable+'\nimport os,time\nfrom pathlib import Path\nPath('+repr(str(pidfile))+').write_text(str(os.getpid()))\ntime.sleep(20)\n')
            binary.chmod(0o700);cancel=threading.Event();errors=[]
            def run():
                try:server.transcribe_pcm(b'aa'*8000,{'temp_directory':directory,'whisper_binary':str(binary),'whisper_model':'test'},cancel)
                except server.WhisperFailure:errors.append('cancelled')
            thread=threading.Thread(target=run,daemon=True);thread.start()
            for _ in range(100):
                if pidfile.exists():break
                time.sleep(.01)
            self.assertTrue(pidfile.exists())
            cancel.set();thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors,['cancelled'])
            with self.assertRaises(ProcessLookupError):os.kill(int(pidfile.read_text()),0)

    def test_echo_similarity(self):
        self.assertTrue(server.is_echo('这是一段测试的话',['这是一段测试的话。']))
        self.assertFalse(server.is_echo('好',['今天的天气很好']))


if __name__=='__main__':unittest.main()

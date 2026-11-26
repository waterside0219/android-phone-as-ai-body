"""Real loopback HTTP/WebSocket recovery; no phone/provider credentials or traffic."""
import asyncio
import ast
import base64
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4
from aiohttp import web, WSMsgType
from aiohttp.test_utils import TestClient, TestServer
import server


class RecoveryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sockets=[]; self.queries=[]; self.created=[]; self.ends=[]; self.received=[]
        self.reject=False
        async def create(request):
            data=await request.json(); self.created.append(data['id']); return web.json_response(data)
        async def end(request):
            self.ends.append(request.match_info['id']); return web.json_response({'ok':True})
        async def stream(request):
            self.queries.append((request.match_info['id'],dict(request.query)))
            if self.reject: return web.Response(status=503)
            ws=web.WebSocketResponse(); await ws.prepare(request); self.sockets.append(ws)
            await ws.send_json({'type':'ready'})
            async for message in ws:
                if message.type==WSMsgType.TEXT: self.received.append(json.loads(message.data))
            return ws
        app=web.Application(); app.router.add_post('/calls',create); app.router.add_post('/calls/{id}/end',end); app.router.add_get('/calls/{id}/stream',stream)
        self.gateway=TestServer(app); await self.gateway.start_server()
        self.app=server.create_app({'gateway':str(self.gateway.make_url(''))},'test-token')
        self.client=TestClient(TestServer(self.app)); await self.client.start_server()
        self.identifier=str(uuid4())
        result=await self.client.post('/api/calls',json={'id':self.identifier}); self.assertEqual(result.status,200)
        self.room=self.app['rooms'][self.identifier]

    async def asyncTearDown(self):
        await self.client.close(); await self.gateway.close()

    async def until(self,predicate,timeout=3):
        async with asyncio.timeout(timeout):
            while not predicate(): await asyncio.sleep(.01)

    async def test_reconnect_same_room_cursors_and_no_duplicate_audio(self):
        ws=self.sockets[-1]
        await ws.send_json({'type':'line','sequence':7,'line':{'id':'ai-1','role':'assistant','text':'本机测试'}})
        await ws.send_json({'type':'audio','generation':1,'audio_sequence':11,'pcm':'AQACAA=='})
        await self.until(lambda:any(e['data'].get('audio_sequence')==11 for e in self.room.events))
        await ws.close()
        await self.until(lambda:len(self.sockets)==2)
        self.assertFalse(self.room.closed)
        self.assertEqual(self.created,[self.identifier],'no replacement call or model turn')
        self.assertEqual(self.ends,[],'transport loss must not send end')
        self.assertEqual(self.queries[-1],(self.identifier,{'after':'7','audio_after':'11'}))
        await self.sockets[-1].send_json({'type':'audio','generation':1,'audio_sequence':11,'pcm':'AQACAA=='})
        await self.sockets[-1].send_json({'type':'audio','generation':1,'audio_sequence':12,'pcm':'AwAEAA=='})
        await self.until(lambda:any(e['data'].get('audio_sequence')==12 for e in self.room.events))
        self.assertEqual([e['data']['audio_sequence'] for e in self.room.events if e['data']['type']=='audio'],[11,12])
        self.assertTrue(any(e['data'].get('state')=='recovering' for e in self.room.events))

    async def test_hangup_cancels_retries_and_does_not_create_another_room(self):
        self.reject=True; await self.sockets[-1].close()
        await self.until(lambda:any(e['data'].get('state')=='recovering' for e in self.room.events))
        await self.client.post(f'/api/calls/{self.identifier}/end')
        count=len(self.queries); await asyncio.sleep(1.1)
        self.assertEqual(len(self.queries),count); self.assertTrue(self.room.closed)
        self.assertEqual(self.created,[self.identifier])

    async def test_remote_normal_hangup_never_reconnects(self):
        await self.sockets[-1].send_json({'type':'call','state':'ended','sequence':3})
        await self.until(lambda:self.room.closed); await asyncio.sleep(.05)
        self.assertEqual(len(self.queries),1)
        self.assertFalse(any(e['data'].get('state')=='recovering' for e in self.room.events))

    async def test_expiry_has_visible_reason_and_gateway_aligned_production_budget(self):
        self.assertEqual(getattr(server,'RECOVERY_SECONDS',None),90)  # 10-30 与网关 90 对齐
        # Accelerate only this loopback failure scenario; production constant remains 45.
        with patch.object(server,'RECOVERY_SECONDS',.15):
            self.reject=True; await self.sockets[-1].close(); await self.until(lambda:self.room.closed)
        errors=[e['data'] for e in self.room.events if e['data']['type']=='error']
        self.assertTrue(any(e.get('code')=='reconnect_failed' and '重连' in e.get('message','') for e in errors))
        self.assertEqual(self.created,[self.identifier]); self.assertEqual(self.ends,[self.identifier])

    async def test_initial_and_reconnect_allow_fifteen_seconds_for_pong(self):
        tree=ast.parse(Path(server.__file__).read_text())
        calls=[n for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='ws_connect']
        self.assertEqual(len(calls),2)
        self.assertEqual([next(ast.literal_eval(k.value) for k in n.keywords if k.arg=='heartbeat') for n in calls],[30,30])

    async def test_recovered_stream_can_still_receive_local_controls(self):
        await self.sockets[-1].close(); await self.until(lambda:len(self.sockets)==2)
        response=await self.client.post(f'/api/calls/{self.identifier}/events',json={'type':'media_ready'})
        self.assertEqual(response.status,200)
        await self.until(lambda:any(m['type']=='media_ready' for m in self.received))

    async def test_reconnect_sends_media_ready_before_waiting_submission(self):
        await self.sockets[-1].close()
        await self.until(lambda:self.room.recovering)
        control=asyncio.create_task(self.room.forward({'type':'submit','id':'u-1','text':'本机测试','manual':True}))
        await control
        await self.until(lambda:any(m['type']=='submit' for m in self.received))
        kinds=[m['type'] for m in self.received]
        self.assertIn('media_ready',kinds,'restore gateway call state before releasing waiting controls')
        self.assertLess(kinds.index('media_ready'),kinds.index('submit'))

    async def test_retry_of_accepted_utterance_end_transcribes_only_once(self):
        with patch.object(self.room,'transcribe',new_callable=AsyncMock) as transcribe:
            await self.room.send({'type':'utterance_start','id':'u-1'})
            await self.room.send({'type':'utterance_audio','id':'u-1','pcm':base64.b64encode(bytes(8000)).decode()})
            ending={'type':'utterance_end','id':'u-1','discard':False}
            await self.room.send(ending)
            await self.room.send(ending)  # First response was lost, same request is retried.
            await asyncio.sleep(0)
            self.assertEqual(transcribe.await_count,1)
            await self.room.send({'type':'utterance_start','id':'u-2'})
            await self.room.send(ending)
            self.assertEqual(self.room.utterance['id'],'u-2','old completion must not consume new capture')


if __name__=='__main__': unittest.main()

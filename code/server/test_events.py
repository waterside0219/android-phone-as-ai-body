import sys, threading, time, unittest
from pathlib import Path
from http.server import ThreadingHTTPServer
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'phone-agent'))
import agent
import body_relay as relay

class RelayEventsTest(unittest.TestCase):
    def test_presence_and_controls_cross_existing_websocket(self):
        relay.HUB=relay.PhoneHub()
        relay.DEVICE_TOKEN=b'local-test-only'
        server=ThreadingHTTPServer(('127.0.0.1',0),relay.Handler)
        server.daemon_threads=True
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        client=agent.connect({'url':'ws://127.0.0.1:%s/agent'%server.server_port,'token':'local-test-only','device_id':'test'})
        try:
            sender=agent.EventSender();sender.attach(client)
            sender.send('presence',{'kind':'arrive'})
            sender.send('controls',{'camera_enabled':False})
            sender.send('message',{'text':'程序测试：500字内回复'})
            deadline=time.monotonic()+1
            while time.monotonic()<deadline and len(relay.HUB.events_after(0)[0])<3: time.sleep(.02)
            self.assertEqual([r['event'] for r in relay.HUB.events_after(0)[0]],['presence','controls','message'])
            sender.send('message',{'text':'字'*501})
            with self.assertRaises(ConnectionError):
                client.receive()
            self.assertEqual(len(relay.HUB.events_after(0)[0]),3,'oversize text must not enter the conversation')
        finally:
            client.sock.close();server.shutdown();server.server_close()
if __name__=='__main__':unittest.main()

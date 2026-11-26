"""v6 pipeline using synthetic frames and model replies; never uses a real key."""
import base64
import importlib.util
import json
import io
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

SPEC = importlib.util.find_spec('body_vision')
if SPEC:
    import body_vision as vision

JPEG = base64.b64encode(b'\xff\xd8synthetic\xff\xd9').decode()


class VisionTest(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(SPEC, 'stdlib vision pipeline is missing')
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.now = 1800000000.0
        self.store = vision.Store(Path(self.tmp.name), clock=lambda: self.now)
        self.description = '有人坐在桌前看屏幕。'
        self.score = .9
        self.sent = []
        self.engine = vision.Engine(self.store, lambda frame, detailed: self.description,
                                    lambda context: self.score, lambda message: self.sent.append(message) or True,
                                    clock=lambda: self.now)

    def packet(self, reason='changed', **extra):
        return {'jpeg_base64': JPEG, 'dhash': '0'*16, 'captured_at': self.now, 'reason': reason,
                'presence': True, 'absent_ms': 0, 'stationary_ms': 0,
                'notify_sadness': True, 'companion_until': 0, **extra}

    def step(self, reason='changed', **extra):
        return self.engine.process(self.packet(reason, **extra))

    def test_transport_failure_codes_preserve_timeout_and_hide_upstream(self):
        self.assertTrue(hasattr(vision,'VisionFailure'),'transport failures still lose their cause')
        cases=[(urllib.error.HTTPError('https://unused',429,'PRIVATE_BODY',{},io.BytesIO(b'PRIVATE_BODY')),'model_http'),
               (TimeoutError('PRIVATE_BODY'),'model_timeout'),
               (urllib.error.URLError(TimeoutError('PRIVATE_BODY')),'model_timeout'),
               (urllib.error.URLError('PRIVATE_BODY'),'model_http')]
        for error,code in cases:
            with self.subTest(code=code),patch('body_vision.urllib.request.build_opener') as build:
                build.return_value.open.side_effect=error
                with self.assertRaises(vision.VisionFailure) as caught:
                    vision.request_json('https://unused','UNUSED_TOKEN')
                self.assertEqual(caught.exception.code,code)
                self.assertNotIn('PRIVATE',str(caught.exception))
        for raw,limit in ((b'PRIVATE_BODY',128),(b'x'*10,8)):
            with patch('body_vision.urllib.request.build_opener') as build:
                build.return_value.open.return_value.__enter__.return_value.read.return_value=raw
                with self.assertRaises(vision.VisionFailure) as caught:
                    vision.request_json('https://unused','UNUSED_TOKEN',limit=limit)
                self.assertEqual(caught.exception.code,'reply_invalid')

    def test_reply_and_classifier_json_have_different_reason_codes(self):
        self.assertTrue(hasattr(vision,'VisionFailure'))
        provider=vision.Provider('unused',self.store,clock=lambda:self.now)
        for reply in ({}, {'choices':[]}, {'choices':[{'message':{'content':None},'finish_reason':'length'}]},
                      {'choices':[{'message':{'content':''},'finish_reason':'length'}]},
                      {'choices':[{'message':{'content':[{'type':'text','text':'PRIVATE_BODY'}]}}]}):
            self.now+=61;provider.request=Mock(return_value=reply)
            with self.assertRaises(vision.VisionFailure) as caught:provider.classify_gesture(self.packet())
            self.assertEqual(caught.exception.code,'reply_invalid')
            self.assertNotIn('PRIVATE',str(caught.exception))
        for content in ('```json\n{"gesture":"wave","doing":"other","confidence":0.9}\n```',
                        '{"gesture":"wave"',
                        '{"gesture":"wave","doing":"other","confidence":true}',
                        '{"gesture":"wave","doing":"other","confidence":NaN}'):
            self.now+=61;provider.request=Mock(return_value={'choices':[{'message':{'content':content},'finish_reason':'stop'}]})
            with self.assertRaises(vision.VisionFailure) as caught:provider.classify_gesture(self.packet())
            self.assertEqual(caught.exception.code,'json_invalid')
            self.assertEqual(str(caught.exception),'json_invalid')

    def test_http_failure_keeps_only_status_code_and_provider(self):
        cases=[(429,429,{'provider_name':'Google AI Studio'},None,'429','Google AI Studio'),
               (400,'invalid_request_error',{'provider_name':'Google Vertex'},None,'invalid_request_error','Google Vertex'),
               (502,502,{},'DeepInfra','502','DeepInfra'),
               (503,None,{},None,'unknown','unknown')]
        for status,code,metadata,provider,want_code,want_provider in cases:
            payload={'error':{'code':code,'message':'PRIVATE_MESSAGE',
                'metadata':{**metadata,'raw':'PRIVATE_RAW','provider_code':'PRIVATE_NATIVE_CODE'}},
                'provider':provider,'user_id':'PRIVATE_USER'}
            stream=io.BytesIO(json.dumps(payload).encode())
            error=urllib.error.HTTPError('https://unused',status,'PRIVATE_REASON',{},stream)
            with self.subTest(status=status),patch('body_vision.urllib.request.build_opener') as build:
                build.return_value.open.side_effect=error
                with self.assertRaises(vision.VisionFailure) as caught:
                    vision.request_json('https://unused','UNUSED_TOKEN')
                self.assertEqual(str(caught.exception),f'model_http http_status={status} error_code={want_code} provider="{want_provider}"')
                self.assertEqual(caught.exception.code,'model_http')
                self.assertTrue(stream.closed)

    def test_http_diagnostics_survive_bad_bodies_and_reject_log_injection(self):
        bodies=[b'PRIVATE_HTML',b'',b'[]',b'null',b'{"error":null}',b'{"error":[]}',
                b'{"error":{"code":{},"metadata":[]}}',
                json.dumps({'error':{'code':'429\nPRIVATE_INJECTION',
                    'metadata':{'provider_name':'Google\nPRIVATE_INJECTION'}}}).encode(),
                json.dumps({'error':{'code':True,'metadata':{'provider_name':['PRIVATE']}}}).encode(),
                json.dumps({'error':{'code':'x'*65,'metadata':{'provider_name':'x'*81}}}).encode(),
                b'x'*(16*1024+1),TimeoutError('PRIVATE_READ_ERROR')]
        for body in bodies:
            stream=Mock()
            if isinstance(body,Exception):stream.read.side_effect=body
            else:stream.read.return_value=body
            error=urllib.error.HTTPError('https://unused',500,'PRIVATE_REASON',{},stream)
            with self.subTest(body_type=type(body).__name__),patch('body_vision.urllib.request.build_opener') as build:
                build.return_value.open.side_effect=error
                with self.assertRaises(vision.VisionFailure) as caught:
                    vision.request_json('https://unused','UNUSED_TOKEN')
                self.assertEqual(str(caught.exception),'model_http http_status=500 error_code=unknown provider="unknown"')
                stream.read.assert_called_once_with(16*1024+1)
                stream.close.assert_called_once()

    def test_http_fields_reach_gesture_log_without_changing_request_or_cleanup(self):
        key=Path(self.tmp.name)/'unused-key';key.write_text('UNUSED_TOKEN')
        provider=vision.Provider(key,self.store,clock=lambda:self.now)
        self.engine.classify=provider.classify_gesture
        self.engine.gesture_result=Mock(return_value={'ok':True})
        packet=self.packet('gesture_candidate',candidate_id='http-error')
        payload={'error':{'code':400,'message':'PRIVATE_BODY',
            'metadata':{'provider_name':'Google AI Studio','raw':'PRIVATE_RAW'}}}
        error=urllib.error.HTTPError('https://unused',400,'PRIVATE_REASON',{},io.BytesIO(json.dumps(payload).encode()))
        with patch('body_vision.relay_frame',return_value=packet),patch('body_vision.urllib.request.build_opener') as build:
            build.return_value.open.side_effect=error
            stage=vision.poll_frame({},self.engine,True)
        self.assertEqual(stage,'request_failed reason=model_http http_status=400 error_code=400 provider="Google AI Studio"')
        self.assertEqual(packet,{})
        self.assertEqual(self.engine.gesture_result.call_args.args[0]['error'],'classification_failed')
        self.assertEqual(self.store.usage[0]['gesture_failed'],1)
        self.assertEqual(build.return_value.open.call_args.kwargs,{'timeout':2.5})
        request=build.return_value.open.call_args.args[0]
        self.assertEqual(json.loads(request.data)['response_format'],{'type':'json_object'})
        self.assertNotIn('PRIVATE',''.join(p.read_text() for p in Path(self.tmp.name).glob('*')))

    def test_exact_request_reply_shapes(self):
        # User supplied four deidentified VPS replies on 2026-10-29, from the
        # exact classify_gesture request and test JPEGs (not phone candidates).
        # Only the supplied content/shape is replayed; no real image/key/cost.
        provider=vision.Provider('unused',self.store,clock=lambda:self.now)
        for confidence in (1,1,.9,1):
            content='{"gesture":"none","doing":"other","confidence":'+str(confidence)+'}'
            response={'model':'google/gemini-2.5-flash-lite','provider':'Google AI Studio',
                'system_fingerprint':None,'choices':[{'index':0,'logprobs':None,
                'finish_reason':'stop','native_finish_reason':'STOP','message':{
                    'role':'assistant','content':content,'refusal':None,'reasoning':None}}]}
            provider.request=Mock(return_value=response)
            self.assertEqual(provider.classify_gesture(self.packet()),
                             {'gesture':'none','doing':'other','confidence':confidence})
            body=provider.request.call_args.args[0]
            self.assertEqual(body['max_tokens'],100)
            self.assertEqual(body['temperature'],0)
            self.assertEqual(body['response_format'],{'type':'json_object'})
            self.assertEqual(provider.request.call_args.kwargs,{'timeout':2.5})
        self.assertEqual(self.store.usage[0]['gesture_failed'],0)

    def test_relay_failure_phase_distinguishes_fetch_metadata_and_result(self):
        self.assertTrue(hasattr(vision,'VisionFailure'))
        token=Path(self.tmp.name)/'token';token.write_text('UNUSED_TOKEN')
        config={'relay_url':'http://127.0.0.1:8832','token_file':str(token)}
        for args,phase in (({'mode':'gesture'},'candidate'),({'mode':'metadata'},'metadata'),
                           ({'gesture_result':{}},'result')):
            with self.subTest(phase=phase),patch('body_vision.request_json',side_effect=ValueError('PRIVATE_BODY')):
                with self.assertRaises(vision.VisionFailure) as caught:vision.relay_frame(config,args)
                self.assertEqual((caught.exception.code,caught.exception.phase),('frame_fetch',phase))

    def test_worker_reports_codes_and_cleanup_cannot_hide_original_failure(self):
        self.assertTrue(hasattr(vision,'poll_frame'),'one worker iteration is not independently verifiable')
        packet=self.packet('gesture_candidate',candidate_id='failed')
        self.engine.classify=Mock(side_effect=vision.VisionFailure('json_invalid'))
        self.engine.gesture_result=Mock(side_effect=vision.VisionFailure('frame_fetch','result'))
        trace=[];self.engine.trace=trace.append
        with patch('body_vision.relay_frame',return_value=packet):
            stage=vision.poll_frame({},self.engine,True)
        self.assertEqual(stage,'request_failed reason=json_invalid')
        self.assertEqual(trace,['gesture_cleanup_failed reason=frame_fetch phase=result'])
        self.assertEqual(packet,{},'failed iteration releases image bytes')
        for code in ('model_http','model_timeout','reply_invalid','json_invalid'):
            packet=self.packet('gesture_candidate',candidate_id=code)
            self.engine.classify=Mock(side_effect=vision.VisionFailure(code))
            self.engine.gesture_result=Mock(return_value={'ok':True})
            with patch('body_vision.relay_frame',return_value=packet):
                self.assertEqual(vision.poll_frame({},self.engine,True),'request_failed reason='+code)
        with patch('body_vision.relay_frame',side_effect=vision.VisionFailure('frame_fetch','candidate')):
            self.assertEqual(vision.poll_frame({},self.engine,True),'request_failed reason=frame_fetch phase=candidate')
        self.engine.classify=Mock(side_effect=ValueError('PRIVATE_INTERNAL_DETAIL'))
        with patch('body_vision.relay_frame',return_value=self.packet('gesture_candidate',candidate_id='internal')):
            self.assertEqual(vision.poll_frame({},self.engine,True),'request_failed reason=internal')

    def test_gesture_classifier_strict_json_and_existing_model(self):
        provider=vision.Provider('unused',self.store,clock=lambda:self.now)
        self.assertTrue(hasattr(provider,'classify_gesture'),'candidate classification is missing')
        for name in ('heart_two_hand','heart_finger','wave','thumb_up','victory','none'):
            reply={'gesture':name,'doing':'other','confidence':.9}
            provider.request=Mock(return_value={'choices':[{'message':{'content':json.dumps(reply)}}], 'usage':{'cost':.00001}})
            self.assertEqual(provider.classify_gesture(self.packet()),reply)
            self.assertEqual(provider.request.call_args.args[0]['model'],vision.MODELS[0])
        self.assertEqual(self.store.usage[0]['gesture_calls'],6)
        self.assertAlmostEqual(self.store.usage[0]['gesture_estimated_usd'],.00006)
        with self.assertRaises(vision.RateLimited):provider.classify_gesture(self.packet())
        for reply in ('```json\n{}\n```','[]','{"gesture":"wave","doing":"other","confidence":true}',
                      '{"gesture":"wave","doing":"other","confidence":NaN}',
                      '{"gesture":"execute","doing":"other","confidence":1}'):
            self.now+=61
            provider.request=Mock(return_value={'choices':[{'message':{'content':reply}}]})
            with self.assertRaises(ValueError):provider.classify_gesture(self.packet())

    def test_candidate_bypasses_notebook_judge_and_returns_none_or_positive(self):
        self.assertTrue(hasattr(self.engine,'process_gesture'),'gesture delivery lane is missing')
        delivered=[]
        self.engine.classify=Mock(return_value={'gesture':'none','doing':'holding_phone','confidence':.9})
        self.engine.gesture_result=lambda result:delivered.append(result) or {'ok':True}
        for name in ('none','heart_finger','heart_two_hand','wave','thumb_up','victory'):
            self.now+=4
            self.engine.classify.return_value={'gesture':name,'doing':'other','confidence':.9}
            packet=self.packet('gesture_candidate',candidate_id=str(self.now),camera_generation='g')
            self.assertEqual(self.engine.process_gesture(packet),'gesture_result')
            self.assertEqual(self.engine.process_gesture(packet),'skip_duplicate')
        self.assertEqual(len(delivered),6)
        self.assertEqual(self.store.notes(),[]);self.assertEqual(self.store.images(),[]);self.assertEqual(self.sent,[])
        self.assertNotIn(JPEG,''.join(p.read_text() for p in Path(self.tmp.name).glob('*')))

    def test_candidate_current_check_failure_and_staleness_never_fabricate_none(self):
        self.assertTrue(hasattr(self.engine,'process_gesture'))
        self.engine.classify=Mock(return_value={'gesture':'wave','doing':'other','confidence':.99})
        self.engine.gesture_result=Mock()
        self.engine.current=lambda:{}
        self.assertEqual(self.engine.process_gesture(self.packet('gesture_candidate',candidate_id='1')), 'gate_closed_after_vision')
        self.engine.gesture_result.assert_not_called()
        self.now+=4;self.engine.current=None
        self.engine.classify.side_effect=ValueError('provider unavailable')
        with self.assertRaises(ValueError):self.engine.process_gesture(self.packet('gesture_candidate',candidate_id='2'))
        self.assertEqual(self.engine.gesture_result.call_args.args[0]['error'],'classification_failed')
        self.assertNotIn('gesture',self.engine.gesture_result.call_args.args[0])
        self.now+=12
        self.assertEqual(self.engine.process_gesture(self.packet('gesture_candidate',candidate_id='3',captured_at=self.now-11)),'gate_invalid_frame')

    def test_successful_classification_still_cleans_up_on_metadata_failure(self):
        self.engine.classify=Mock(return_value={'gesture':'wave','doing':'other','confidence':.9})
        self.engine.current=Mock(side_effect=ValueError('metadata temporarily unavailable'))
        self.engine.gesture_result=Mock()
        with self.assertRaises(ValueError):self.engine.process_gesture(self.packet('gesture_candidate',candidate_id='c'))
        self.assertTrue(self.engine.gesture_result.called,'metadata failure must still release the phone frame')
        self.assertEqual(self.engine.gesture_result.call_args.args[0]['error'],'classification_failed')

    def test_ordinary_and_gesture_budgets_cannot_starve_each_other(self):
        provider=vision.Provider('unused',self.store,clock=lambda:self.now)
        provider.request=Mock(return_value={'choices':[{'message':{'content':'{"gesture":"none","doing":"other","confidence":1}'}}]})
        self.store.state['calls']=[self.now-120]*120
        provider.classify_gesture(self.packet())
        self.store.state['calls']=[self.now-120]*60
        self.store.state['gesture_calls']=[self.now-120]*60
        self.assertTrue(provider.call({'model':vision.JUDGE}))

    def test_gesture_api_budget_is_sixty_per_hour_and_survives_restart(self):
        provider=vision.Provider('unused',self.store,clock=lambda:self.now)
        self.assertTrue(hasattr(provider,'classify_gesture'))
        provider.request=Mock(return_value={'choices':[{'message':{'content':'{"gesture":"none","doing":"other","confidence":1}'}}]})
        for i in range(60):
            if i and i%6==0:self.now+=61
            provider.classify_gesture(self.packet())
        self.now+=61
        restored=vision.Store(Path(self.tmp.name),clock=lambda:self.now)
        other=vision.Provider('unused',restored,clock=lambda:self.now)
        other.request=Mock()
        with self.assertRaises(vision.RateLimited):other.classify_gesture(self.packet())
        other.request.assert_not_called()

    def test_notebook_is_not_a_notification_and_expires_without_new_frames(self):
        self.score = .1; self.step()
        self.assertEqual(self.sent, [])
        self.assertEqual(self.store.notes()[0]['description'], self.description)
        self.assertIn('+08:00', self.store.notes()[0]['time'])
        self.assertEqual(self.store.images(), [])
        self.now += 7200; self.store.prune()
        self.assertEqual(self.store.notes(), [])
        self.assertNotIn(self.description, self.store.note_path.read_text())

    def test_five_minute_gap_without_hourly_cap(self):
        for delta in (0, 60, 300, 300, 300):
            self.now += delta; self.step()
        self.assertEqual(len(self.sent), 4)

    def test_all_fast_events_are_exempt_and_deduplicated(self):
        self.store.state['pushes'] = [self.now-100, self.now-200, self.now-300]
        self.score = 0
        for description in ('拿出一碗面条准备吃。', '冲着镜头挥手打招呼。', '趴在桌上闭眼不动。', '低头抹眼睛，像在哭。'):
            self.description = description; self.now += 60; self.step()
            count = len(self.sent); self.now += 60; self.step()
            self.assertEqual(len(self.sent), count, 'same continuous event must not spam')
        self.assertEqual(len(self.sent), 4)
        self.assertEqual(len(self.store.state['pushes']), 3)
        self.assertTrue(all(m.startswith('这是识图报的，不是用户发的') for m in self.sent))

    def test_sadness_opt_out_and_other_sensitive_content_never_stored(self):
        for description in ('有人哭着捂脸。', '有人难过地抹眼睛。', '有人穿睡衣。', '有人去医院。'):
            self.description = description; self.now += 60
            self.step('manual', notify_sadness=False)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.store.notes(), [])
        self.assertEqual(self.store.images(), [])
        self.description = '有人哭着捂脸。'; self.now += 60; self.step('manual')
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.store.notes(), [])
        self.assertEqual(self.store.images(), [])

    def test_return_absence_and_stationary_use_phone_metadata_without_cloud(self):
        self.engine.describe = Mock(side_effect=AssertionError('no image, no cloud'))
        absent = {'presence': False, 'seen_person': True, 'absent_ms': 1200001, 'stationary_ms': 0}
        self.engine.process(absent); self.engine.process(absent)
        self.assertEqual(len(self.sent), 1)
        returned = {'presence': True, 'seen_person': True, 'absent_ms': 1200001, 'returned_at': self.now, 'stationary_ms': 0}
        self.engine.process(returned); self.engine.process(returned)
        self.assertEqual(len(self.sent), 2)
        returned['stationary_ms'] = 3600001
        self.engine.process(returned); self.engine.process(returned)
        self.assertEqual(len(self.sent), 3)

    def test_manual_and_companion_send_details_and_image_is_only_pulled(self):
        self.score = 0
        for reason in ('manual', 'companion'):
            self.now += 240
            self.step(reason, companion_until=self.now+3600)
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.store.state['pushes'], [])
        self.assertTrue(all(JPEG not in message for message in self.sent))
        images = self.store.images()
        self.assertEqual(len(images), 2)
        self.assertEqual(self.store.image(images[-1]['id']), base64.b64decode(JPEG))
        self.now += 600; self.store.prune()
        self.assertEqual(self.store.images(), [])
        with self.assertRaises(ValueError):self.store.image(images[-1]['id'])

    def test_closed_during_model_call_drops_result_and_opt_out_is_rechecked(self):
        self.engine.current = lambda: {}
        self.step('manual')
        self.assertEqual(self.sent, []); self.assertEqual(self.store.notes(), [])
        self.assertEqual(self.store.images(), [])
        self.description='有人哭着捂脸。'
        self.engine.current=lambda: {'presence':True,'notify_sadness':False}
        self.now+=60;self.step('manual')
        self.assertEqual(self.sent, []);self.assertEqual(self.store.notes(), [])

    def test_privacy_close_reopen_and_camera_restart_reject_inflight_old_frame(self):
        self.engine.current=lambda: {'presence':True,'frame_cutoff':self.now+1,'camera_generation':'new'}
        self.step('manual', camera_generation='old')
        self.assertEqual(self.sent, [])
        self.assertEqual(self.store.notes(), [])
        self.assertEqual(self.store.images(), [])

    def test_duplicate_frame_and_recent_speech_do_not_spend_or_interrupt(self):
        describe = Mock(return_value=self.description); self.engine.describe = describe
        packet=self.packet(last_say_at=self.now-30)
        self.engine.process(packet);self.engine.process(packet)
        self.assertEqual(describe.call_count,1);self.assertEqual(self.sent,[])

    def test_restart_does_not_repeat_fast_notifications_or_pay_for_same_frame(self):
        self.description='趴在桌上闭眼不动。'
        packet=self.packet(returned_at=self.now,absent_ms=1200001)
        self.engine.process(packet)
        self.assertEqual(len(self.sent),2)
        restored=vision.Store(Path(self.tmp.name),clock=lambda:self.now)
        describe=Mock(side_effect=AssertionError('already paid for this frame'))
        engine=vision.Engine(restored,describe,lambda _:0,lambda message:self.sent.append(message) or True,clock=lambda:self.now)
        engine.process(packet)
        self.assertEqual(len(self.sent),2)

    def test_provider_budget_fallback_and_sanitized_errors(self):
        provider = vision.Provider('unused-key-path', self.store, clock=lambda: self.now)
        self.assertFalse(Path('unused-key-path').exists())
        provider.request = Mock(side_effect=[ValueError('private upstream body'), {'choices':[{'message':{'content':'桌旁有人。'}}], 'usage':{'cost':.00001}}])
        self.assertEqual(provider.describe(self.packet(), False), '桌旁有人。')
        self.assertEqual([c.args[0]['model'] for c in provider.request.call_args_list],
                         ['google/gemini-2.5-flash-lite', 'qwen/qwen3-vl-30b-a3b-instruct'])
        # API attempts (including fallback and judge) share the stricter global budget.
        self.assertEqual(len(self.store.state['calls']),2)
        provider.request = Mock(return_value={'choices':[{'message':{'content':'0.8'}}], 'usage':{'cost':.00001}})
        provider.request_decisions = Mock(return_value={'answers':{'speak':{'probabilities':{'0':.1,'1':.1,'2':.5,'3':.3}}}, 'usage':{'cost':.00001}})
        self.assertAlmostEqual(provider.judge({}), .8);provider.judge({})
        self.assertEqual(provider.request_decisions.call_args.args[0]['model'], 'typesafe/jev-1.13')
        with self.assertRaises(vision.RateLimited):provider.judge({})
        self.now+=61;self.store.state['calls']=[self.now-120]*120
        with self.assertRaises(vision.RateLimited):provider.judge({})
        self.assertNotIn('private upstream body', ''.join(p.read_text() for p in Path(self.tmp.name).glob('*.json*')))

    def test_jev_failure_falls_back_to_text_judge(self):
        provider = vision.Provider('unused-key-path', self.store, clock=lambda: self.now)
        provider.request_decisions = Mock(side_effect=ValueError('upstream'))
        provider.request = Mock(return_value={'choices':[{'message':{'content':'0.2'}}], 'usage':{'cost':.00001}})
        self.assertEqual(provider.judge({}), .2)
        self.assertEqual(provider.request.call_args.args[0]['model'], 'openai/gpt-5-nano')
        provider.request_decisions = Mock(return_value={'answers':{'speak':{'probabilities':{'2':'x'}}}})
        self.assertEqual(provider.judge({}), .2)  # Malformed Jev answer also falls back.

    def test_bad_scores_and_negative_greeting_do_not_push(self):
        self.description='有人坐着，没有挥手，也没有哭。'
        for score in (float('nan'), -1, 1.2, None):
            self.now+=60;self.score=score;self.step()
        self.assertEqual(self.sent, [])

    def test_tmux_uses_literal_buffer_and_refuses_shell_pane(self):
        config={'tmux_target':'ai:0.0','tmux_command':'claude'}
        with patch('body_vision.subprocess.run') as run:
            run.return_value=Mock(returncode=0,stdout=b'bash\n')
            self.assertFalse(vision.notify_tmux(config, 'untrusted $(data)'))
            self.assertEqual(run.call_count,1)
            run.reset_mock();run.return_value=Mock(returncode=0,stdout=b'claude\n')
            self.assertTrue(vision.notify_tmux(config, 'untrusted $(data)'))
            self.assertEqual(run.call_count,4)
            self.assertEqual(run.call_args_list[1].kwargs['input'],b'untrusted $(data)')
            self.assertTrue(all(not c.kwargs.get('shell') for c in run.call_args_list))

    def test_configuration_cannot_target_shell_or_nonlocal_relay(self):
        path=Path(self.tmp.name)/'config.json'
        config={'token_file':'token','api_key_file':'key','state_dir':'state',
                'tmux_target':'test:0.0','tmux_command':'bash'}
        path.write_text(json.dumps(config))
        with self.assertRaises(ValueError):vision.read_config(path)
        config.update(tmux_command='claude',relay_url='https://untrusted.example')
        path.write_text(json.dumps(config))
        with self.assertRaises(ValueError):vision.read_config(path)


if __name__=='__main__':unittest.main()

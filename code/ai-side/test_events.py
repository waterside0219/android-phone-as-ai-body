import unittest
import events


class SummarizeTest(unittest.TestCase):
    def test_merges_touches_and_skips_noise(self):
        state = {}
        text = events.summarize([
            {"event": "touch", "args": {"kind": "tap"}}, {"event": "touch", "args": {"kind": "tap"}},
            {"event": "presence", "args": {"kind": "looking"}},
            {"event": "presence", "args": {"kind": "arrive"}},
            {"event": "gesture", "args": {"name": "heart"}},
            {"event": "gesture", "args": {"name": "wave"}},
            {"event": "message", "args": {"text": " 在吗 "}},
        ], state, now=1000)
        self.assertTrue(text.startswith("戳了你2下、回来了"))
        self.assertIn("冲你挥手", text)
        self.assertIn("「在吗」", text)
        self.assertNotIn("比心", text)
        self.assertNotIn("在看你", text)

    def test_same_gesture_and_camera_loss_are_throttled(self):
        state = {}
        self.assertIn("挥手", events.summarize([{"event": "gesture", "args": {"name": "wave"}}], state, now=1000))
        self.assertEqual(events.summarize([{"event": "gesture", "args": {"name": "wave"}}], state, now=1500), "")
        self.assertIn("丢了", events.summarize([{"event": "camera_lost", "args": {}}], state, now=1000))
        self.assertEqual(events.summarize([{"event": "camera_lost", "args": {}}], state, now=2000), "")
        events.summarize([{"event": "shizuku_back", "args": {}}], state, now=2100)
        self.assertIn("丢了", events.summarize([{"event": "camera_lost", "args": {}}], state, now=2200))

    def test_controls_are_remembered_not_reported(self):
        state = {}
        self.assertEqual(events.summarize([{"event": "controls", "args": {"muted": True}}], state, now=1), "")
        self.assertEqual(state["controls"], {"muted": True})


if __name__ == "__main__":
    unittest.main()

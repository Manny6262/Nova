import sys, unittest
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'engine'))
from voice_turns import EndpointSchedule, PlaybackEchoFilter
class VoiceTests(unittest.TestCase):
 def test_brief_pause_retains_turn(self):
  e=EndpointSchedule();self.assertFalse(e.finish(1.1,False));self.assertTrue(e.finish(.8,True))
 def test_repeated_checks_after_incomplete_prediction(self):
  e=EndpointSchedule();self.assertFalse(e.check(.3));self.assertTrue(e.check(.65));self.assertFalse(e.check(.9));self.assertTrue(e.check(1.2));e.speech();self.assertTrue(e.check(.65))
 def test_safety_limits_are_bounded(self):
  e=EndpointSchedule();self.assertFalse(e.finish(3.4));self.assertTrue(e.finish(3.5));self.assertTrue(e.finish(0,duration=90))
 def test_echo_is_removed(self):
  rng=np.random.default_rng(4);reference=rng.integers(-6000,6000,1024,dtype=np.int16)
  f=PlaybackEchoFilter();f.add_reference(reference.tobytes());echo=(reference.astype(float)*.6).astype(np.int16)
  residual=np.frombuffer(f.clean(echo.tobytes()),dtype=np.int16).astype(float)
  self.assertLess(np.sqrt(np.mean(residual**2)),5)
 def test_speech_mixed_with_echo_is_retained(self):
  rng=np.random.default_rng(8);ref=rng.integers(-6000,6000,1024,dtype=np.int16);user=rng.integers(-2000,2000,1024,dtype=np.int16)
  mixed=(ref.astype(float)*.8+user).astype(np.int16);f=PlaybackEchoFilter();f.add_reference(ref.tobytes())
  out=np.frombuffer(f.clean(mixed.tobytes()),dtype=np.int16).astype(float)
  self.assertGreater(np.corrcoef(out,user)[0,1],.99);self.assertGreater(np.sqrt(np.mean(out**2)),900)
 def test_no_reference_preserves_speech(self):
  pcm=np.arange(1024,dtype=np.int16).tobytes();self.assertEqual(PlaybackEchoFilter().clean(pcm),pcm)
 def test_uncorrelated_reference_does_not_mute_user(self):
  rng=np.random.default_rng(9);f=PlaybackEchoFilter();f.add_reference(rng.integers(-5000,5000,1024,dtype=np.int16).tobytes());pcm=rng.integers(-6000,6000,1024,dtype=np.int16).tobytes();self.assertEqual(f.clean(pcm),pcm)
 def test_reference_retention_bounded(self):
  f=PlaybackEchoFilter()
  for _ in range(100):f.add_reference(bytes(1024))
  self.assertEqual(len(f.reference),32)
 def test_all_changed_python_compiles(self):
  for name in ['pipecat_service.py','voice_turns.py']:
   p=Path(__file__).resolve().parents[1]/'engine'/name;compile(p.read_text(encoding='utf-8'),str(p),'exec')
if __name__=='__main__':unittest.main(verbosity=2)

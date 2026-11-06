"""Real FFmpeg regression for the 518-beat UMG render failure."""
import importlib.util
from pathlib import Path
import shutil
import subprocess
import unittest

MODULE = Path(__file__).resolve().parents[1] / 'fx_compositor.py'
spec = importlib.util.spec_from_file_location('fx_long_render', MODULE)
fx = importlib.util.module_from_spec(spec)
# dataclasses consult their module while resolving postponed annotations.
import sys
sys.modules[spec.name] = fx
spec.loader.exec_module(fx)


@unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg is required')
class LongReactiveRenderTests(unittest.TestCase):
    def render(self, beats):
        rhythm = fx.EffectRhythm(161.5, beats, tuple(.8 for _ in beats))
        graph = fx.rhythm_mask_graph(rhythm,16,16,raw_label='0:v',out_label='out').rstrip(';')
        result = subprocess.run([
            'ffmpeg','-v','error','-f','lavfi','-i','color=white:s=16x16:r=30',
            '-filter_complex',graph,'-map','[out]','-frames:v','24',
            '-pix_fmt','gray','-f','rawvideo','-',
        ],capture_output=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace')[:1000])
        self.assertEqual(len(result.stdout),24*16*16)
        frames = [result.stdout[i*256:(i+1)*256] for i in range(24)]
        # Scaling the mask must preserve a uniform illumination value.
        self.assertTrue(all(len(set(frame)) == 1 for frame in frames))
        return [frame[0] for frame in frames]

    def test_normal_song_with_518_beats_parses_and_pulses(self):
        values = self.render(tuple(.1+i*.3715 for i in range(518)))
        self.assertGreater(values[3],values[9]+120)
        self.assertGreater(values[14],values[20]+120)

    def test_late_beats_survive_the_long_expression(self):
        # A late term must still contribute; truncating the expression to
        # the first few beats would parse but silently lose this pulse.
        values = self.render(tuple(10+i*.4 for i in range(517))+(.1,))
        self.assertGreater(values[3],values[9]+120)

    def test_maximum_grid_remains_renderable(self):
        values = self.render(tuple(.1+i*.4 for i in range(900)))
        self.assertGreater(values[3],values[9]+120)


if __name__ == '__main__':
    unittest.main()

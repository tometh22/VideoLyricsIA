from pathlib import Path
from types import SimpleNamespace
import unittest

from local_qwen_mlx import transcribe_without_reference


class QwenListenerTests(unittest.TestCase):
    def test_audio_only_and_bounded_generation(self):
        class FakeModel:
            def generate(self, audio, **kwargs):
                self.audio, self.kwargs = audio, kwargs
                return SimpleNamespace(text=" hola ")
        model = FakeModel()
        self.assertEqual(transcribe_without_reference(model, Path("clip.wav")), "hola")
        self.assertEqual(model.audio, "clip.wav")
        self.assertIsNone(model.kwargs["system_prompt"])
        self.assertIsNone(model.kwargs["hotwords"])
        self.assertEqual(model.kwargs["max_tokens"], 384)
        self.assertEqual(model.kwargs["batch_size"], 1)


if __name__ == "__main__":
    unittest.main()

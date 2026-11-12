"""A stand-in for faster_whisper.WhisperModel, so tests need no model files."""
from types import SimpleNamespace


def fake_segment(text, start, end):
    tokens = text.split()
    step = (end - start) / max(len(tokens), 1)
    words = [
        SimpleNamespace(word=" " + t, start=start + i * step, end=start + (i + 1) * step)
        for i, t in enumerate(tokens)
    ]
    return SimpleNamespace(text=" " + text, start=start, end=end, words=words)


DEFAULT_SEGMENTS = [("Hello there.", 0.0, 1.0), ("This is a test.", 1.0, 2.0)]


class FakeWhisper:
    """Records how it was built and called. Class attributes steer its failures."""

    built = []            # (model_id, device, compute_type) of every construction attempt
    calls = []            # kwargs of every transcribe call
    fail_compute_types = set()   # constructing with one of these raises
    fail_on_device = set()       # transcribing on one of these raises while decoding
    segments = DEFAULT_SEGMENTS
    language = "en"

    @classmethod
    def reset(cls):
        cls.built, cls.calls = [], []
        cls.fail_compute_types, cls.fail_on_device = set(), set()
        cls.segments, cls.language = DEFAULT_SEGMENTS, "en"

    def __init__(self, model_id, device="cpu", compute_type="default"):
        FakeWhisper.built.append((model_id, device, compute_type))
        if compute_type in FakeWhisper.fail_compute_types:
            raise ValueError(f"compute type {compute_type} is not supported")
        self.model_id, self.device = model_id, device

    def transcribe(self, path, **kwargs):
        FakeWhisper.calls.append({"model_id": self.model_id, "device": self.device, **kwargs})
        duration = max(end for _, _, end in FakeWhisper.segments)
        info = SimpleNamespace(
            language=kwargs.get("language") or FakeWhisper.language,
            language_probability=0.97,
            duration=duration,
        )

        def generate():
            for text, start, end in FakeWhisper.segments:
                if self.device in FakeWhisper.fail_on_device:
                    raise RuntimeError("Could not load library cudnn_ops64_9.dll")
                yield fake_segment(text, start, end)

        return generate(), info

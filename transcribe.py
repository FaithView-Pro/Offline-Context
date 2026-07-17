"""Stage 1 -- TRANSCRIPTION.

Local Whisper via ``faster-whisper`` (CTranslate2 backend, CPU ``int8``).
Turns an audio file into a list of timestamped segments::

    [{text, start_time, end_time}, ...]

faster-whisper gives segment-level timestamps by default and optional
word-level timestamps. Both are surfaced; downstream stages only need the
segment fields, but words are retained for finer-grained buffering if desired.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import config


@dataclass
class Word:
    text: str
    start_time: float
    end_time: float
    probability: float = 1.0


@dataclass
class Segment:
    text: str
    start_time: float
    end_time: float
    words: list[Word] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"text": self.text, "start_time": self.start_time, "end_time": self.end_time}


def _load_whisper(model_name: str, offline: bool):
    from faster_whisper import WhisperModel

    if offline:
        import os
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    # int8 is the fastest CPU compute type and needs no AVX512/GPU.
    return WhisperModel(model_name, device="cpu", compute_type="int8")


def transcribe(
    audio_path: str,
    settings: Optional[config.Settings] = None,
    word_timestamps: bool = False,
) -> list[Segment]:
    """Transcribe ``audio_path`` to timestamped :class:`Segment` objects."""
    s = settings or config.Settings()
    model = _load_whisper(s.whisper_model, s.offline)
    segs, _info = model.transcribe(
        audio_path,
        beam_size=s.whisper_beam_size,
        language=s.whisper_language,
        vad_filter=s.whisper_vad_filter,
        word_timestamps=word_timestamps,
    )

    out: list[Segment] = []
    for seg in segs:
        words = []
        if word_timestamps and getattr(seg, "words", None):
            for w in seg.words:
                words.append(Word(w.word.strip(), float(w.start), float(w.end), float(w.probability)))
        out.append(Segment(seg.text.strip(), float(seg.start), float(seg.end), words))
    return out


def segments_from_dicts(dicts: list[dict]) -> list[Segment]:
    """Build Segment objects from plain dicts.

    Lets the downstream pipeline be tested/driven from a pre-existing
    transcript without running Whisper, and lets the real-time console feed
    live captions through the same code path.
    """
    out: list[Segment] = []
    for d in dicts:
        out.append(Segment(
            text=d.get("text", "").strip(),
            start_time=float(d.get("start_time", d.get("start", 0.0))),
            end_time=float(d.get("end_time", d.get("end", 0.0))),
        ))
    return out

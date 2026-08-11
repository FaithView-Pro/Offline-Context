"""Common transcription source interface.

Both Whisper (offline) and Deepgram (online) implement this so the pipeline
can switch between them live without changing buffer.py, intent_router.py,
or any downstream stage.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Optional

from transcribe import Segment


class TranscriptionSource(ABC):
    """A source of live ASR segments.

    ``audio_source`` is any object with a ``.stream() -> Iterator[tuple[np.ndarray, float]]``
    method (see live_transcribe.AudioSource). We accept duck-typing here rather
    than importing AudioSource directly to avoid a circular import between
    transcribe.py and live_transcribe.py.

    ``on_segment`` receives FINALIZED (complete) utterances through the normal
    pipeline (buffer -> intent_router -> quote_detect -> retrieve -> rerank).

    ``on_interim`` (optional) receives partial/interim transcripts for live
    caption display only — bypasses the detection pipeline entirely.  Only
    Deepgram produces these; Whisper sources ignore the parameter.
    """

    @abstractmethod
    def start(
        self,
        audio_source,
        on_segment: Callable[[Segment], None],
        on_interim: Optional[Callable[[Segment], None]] = None,
    ) -> None:
        """Begin transcribing audio from ``audio_source``.

        Each finalized utterance is delivered to ``on_segment`` as a
        :class:`Segment`.  If ``on_interim`` is provided, partial transcripts
        are delivered to it directly as they arrive.
        """
        ...

    @abstractmethod
    def stop(self) -> None:
        """Stop transcription and release resources."""
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """Machine-readable name -- "whisper", "deepgram", etc."""
        ...

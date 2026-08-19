"""Narration audio via Renderful text-to-audio.

One approved narration line in, one speech file out, plus the measured duration
that replaces the word-count estimate.

The failure classes are the image path's, deliberately: 401/402/403/429 and
"limit reached" are fatal for the whole run, other 4xx fail that one line, 5xx
and network errors retry with backoff. `renderful.api_call` owns that ladder --
there is exactly one of it, and this module reuses it rather than growing a
second.

Video models that advertise "native audio" are not an alternative here. They
invent ambience and dialogue from the image prompt; none of them accept an
approved narration line and read it aloud. Pictures move, TTS speaks the script.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import audio, config, engines, renderful


@dataclass(frozen=True)
class Speech:
    """One synthesised line, as delivered."""

    data: bytes
    extension: str
    seconds: float | None
    cost: float | None
    generation_id: str | None
    source_url: str | None

    @property
    def measured(self) -> bool:
        """False when the container could not be parsed and the caller must estimate."""
        return self.seconds is not None


def synthesize(text: str, key: str, model: str, params: dict, *,
               should_stop=None, on_state=None, on_submit=None) -> Speech:
    """Submit one line, wait for it, download it, measure it.

    Raises ParamError before spending anything if the text cannot be sent.

    `on_submit` is called with the generation id the moment the request is
    accepted. Everything after that point is billed whether or not this function
    returns, so the caller's activity record needs to know the id even when the
    poll or the download is what failed.
    """
    line = (text or "").strip()
    if not line:
        raise engines.ParamError("Narration line is empty.")
    if len(line) > config.MAX_TTS_CHARS:
        raise engines.ParamError(
            f"Narration line is {len(line)} characters; the limit is "
            f"{config.MAX_TTS_CHARS}."
        )

    created = renderful.submit(line, key, model, params,
                               gen_type=renderful.GEN_TYPE_AUDIO)
    gen_id = created.get("id")
    if not gen_id:
        raise RuntimeError(f"no generation id in response: {created}")
    if on_submit is not None:
        on_submit(gen_id)

    status_doc = renderful.wait_for(
        gen_id, key,
        should_stop=should_stop,
        on_state=on_state,
        timeout=config.AUDIO_POLL_TIMEOUT,
        interval=config.AUDIO_POLL_SECONDS,
    )

    urls = renderful.outputs_of(status_doc)
    if not urls:
        raise RuntimeError(f"completed with no outputs: {status_doc}")

    data = renderful.download(urls[0])
    kind = renderful.sniff(data)
    return Speech(
        data=data,
        extension=kind if kind != "bin" else "audio",
        seconds=audio.seconds(data),
        cost=status_doc.get("cost", created.get("cost")),
        generation_id=gen_id,
        source_url=urls[0],
    )

from __future__ import annotations

import os
from typing import Dict, List

import requests


DEFAULT_BASE_URL = "http://localhost:1234/v1"


class LMStudioError(RuntimeError):
    pass


def _env_int(name: str, default: int, *, minimum: int = 1, maximum: int = 100000) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


def _env_float(name: str, default: float, *, minimum: float = 0.0, maximum: float = 1.0) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


def list_models(base_url: str = DEFAULT_BASE_URL) -> List[str]:
    url = base_url.rstrip("/") + "/models"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        payload = resp.json()
        return [item.get("id") for item in payload.get("data", []) if item.get("id")]
    except Exception as exc:  # noqa: BLE001
        raise LMStudioError(f"Could not list LM Studio models at {url}: {exc}") from exc


def generate_meeting_notes(
    *,
    transcript_text: str,
    manual_notes: str,
    meeting_title: str,
    model: str,
    base_url: str = DEFAULT_BASE_URL,
    temperature: float = 0.2,
) -> str:
    """Generate structured meeting notes via LM Studio OpenAI-compatible API."""
    # Keep chunks small enough that local models do not spend minutes reasoning over short meetings.
    chunks = _chunk_text(
        transcript_text,
        max_chars=_env_int("LOCAL_MINUTES_TRANSCRIPT_CHUNK_CHARS", 10000, minimum=3000, maximum=40000),
        overlap=_env_int("LOCAL_MINUTES_TRANSCRIPT_CHUNK_OVERLAP", 500, minimum=0, maximum=3000),
    )
    if len(chunks) == 1:
        return _final_notes(
            transcript_or_chunk_notes=chunks[0],
            manual_notes=manual_notes,
            meeting_title=meeting_title,
            model=model,
            base_url=base_url,
            temperature=temperature,
            already_condensed=False,
        )

    chunk_summaries: List[str] = []
    for idx, chunk in enumerate(chunks, start=1):
        chunk_summaries.append(
            _chat_complete(
                model=model,
                base_url=base_url,
                temperature=temperature,
                max_tokens=_env_int("LOCAL_MINUTES_LMSTUDIO_CHUNK_MAX_TOKENS", 700, minimum=128, maximum=4096),
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Condense this meeting transcript chunk into compact factual notes. "
                            "Return only final notes, not reasoning. Preserve decisions, action items, "
                            "owners, dates, risks, blockers, open questions, and important evidence. "
                            "Do not invent missing details. If duplicate MIC/SYSTEM lines appear within a few seconds, use them once."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Meeting title: {meeting_title or 'Untitled meeting'}\n"
                            f"Chunk {idx} of {len(chunks)}:\n\n{chunk}\n\n"
                            "Return concise bullet notes for this chunk only. Maximum 12 bullets."
                        ),
                    },
                ],
            )
        )

    return _final_notes(
        transcript_or_chunk_notes="\n\n".join(
            f"## Chunk {i}\n{summary}" for i, summary in enumerate(chunk_summaries, start=1)
        ),
        manual_notes=manual_notes,
        meeting_title=meeting_title,
        model=model,
        base_url=base_url,
        temperature=temperature,
        already_condensed=True,
    )


def _final_notes(
    *,
    transcript_or_chunk_notes: str,
    manual_notes: str,
    meeting_title: str,
    model: str,
    base_url: str,
    temperature: float,
    already_condensed: bool,
) -> str:
    source_label = "condensed transcript notes" if already_condensed else "transcript"
    return _chat_complete(
        model=model,
        base_url=base_url,
        temperature=temperature,
        max_tokens=_env_int("LOCAL_MINUTES_LMSTUDIO_MAX_TOKENS", 1200, minimum=256, maximum=8192),
        messages=[
            {
                "role": "system",
                "content": (
                    "You create concise, accurate meeting minutes. Return only the final Markdown minutes. "
                    "Do not include thinking, reasoning, analysis, self-checks, hidden notes, or planning text. "
                    "Never invent attendees, owners, due dates, blockers, or decisions. "
                    "If something is unclear, write 'Not specified'. "
                    "If the same sentence appears under both MIC and SYSTEM within a few seconds, treat it as duplicate audio capture and use it only once. "
                    "Keep the output short and useful for business follow-up."
                ),
            },
            {
                "role": "user",
                "content": f"""
Meeting title: {meeting_title or 'Untitled meeting'}

User's rough notes:
{manual_notes.strip() or '(none)'}

Meeting {source_label}:
{transcript_or_chunk_notes.strip()}

Create Markdown meeting minutes with exactly these sections and no extra sections:
# {meeting_title or 'Meeting Minutes'}
## Executive summary
Use 1 to 3 short bullets.
## Key discussion points
Use concise bullets.
## Decisions
Write 'Not specified' if none.
## Action items
Use a Markdown table with columns: Owner, Action, Due date, Evidence.
## Risks and blockers
Write 'Not specified' if none.
## Open questions
Write 'Not specified' if none.
## Follow-up message draft
Write a short email or Slack-style follow-up that can be sent to attendees.
""".strip(),
            },
        ],
    )


def _chat_complete(
    *,
    model: str,
    base_url: str,
    messages: List[Dict[str, str]],
    temperature: float,
    max_tokens: int,
) -> str:
    url = base_url.rstrip("/") + "/chat/completions"
    timeout_seconds = _env_int("LOCAL_MINUTES_LMSTUDIO_TIMEOUT_SECONDS", 300, minimum=30, maximum=1800)

    payload: Dict[str, object] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "top_p": _env_float("LOCAL_MINUTES_LMSTUDIO_TOP_P", 0.9, minimum=0.01, maximum=1.0),
        "stream": False,
        "max_tokens": max_tokens,
    }

    # LM Studio 0.4.8+ supports reasoning_effort and reasoning_tokens on OpenAI-compatible chat completions.
    # These are optional because older LM Studio builds or some models may ignore or reject them.
    reasoning_effort = os.getenv("LOCAL_MINUTES_LMSTUDIO_REASONING_EFFORT", "").strip()
    if reasoning_effort:
        payload["reasoning_effort"] = reasoning_effort

    reasoning_tokens = os.getenv("LOCAL_MINUTES_LMSTUDIO_REASONING_TOKENS", "").strip()
    if reasoning_tokens:
        try:
            payload["reasoning_tokens"] = max(0, int(reasoning_tokens))
        except ValueError:
            pass

    try:
        resp = requests.post(
            url,
            headers={"Authorization": "Bearer lm-studio", "Content-Type": "application/json"},
            json=payload,
            timeout=timeout_seconds,
        )
        resp.raise_for_status()
        data = resp.json()
        message = data["choices"][0]["message"]
        content = (message.get("content") or "").strip()
        if content:
            return content

        # Gemma reasoning-mode failures often return a huge reasoning_content with empty final content.
        # Surface a precise fix instead of silently saving blank notes.
        reasoning = (message.get("reasoning_content") or message.get("reasoning") or "").strip()
        if reasoning:
            raise LMStudioError(
                "LM Studio returned reasoning_content but no final message content. "
                "Turn off Thinking for this model in LM Studio, or set "
                "LOCAL_MINUTES_LMSTUDIO_REASONING_EFFORT=low and "
                "LOCAL_MINUTES_LMSTUDIO_REASONING_TOKENS=64, then try again."
            )
        raise LMStudioError("LM Studio returned an empty response.")
    except LMStudioError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise LMStudioError(f"LM Studio request failed at {url}: {exc}") from exc


def _chunk_text(text: str, max_chars: int, overlap: int) -> List[str]:
    text = text.strip()
    if len(text) <= max_chars:
        return [text]

    chunks = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        # Prefer splitting at a newline near the boundary.
        split_at = text.rfind("\n", start, end)
        if split_at <= start + int(max_chars * 0.65):
            split_at = end

        chunk = text[start:split_at].strip()
        if chunk:
            chunks.append(chunk)
        if split_at >= len(text):
            break

        # Move forward with a small overlap, but always make progress.
        start = max(split_at - overlap, start + 1)
    return chunks

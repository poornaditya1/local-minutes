from __future__ import annotations

from typing import Dict, List, Optional

import requests


DEFAULT_BASE_URL = "http://localhost:1234/v1"


class LMStudioError(RuntimeError):
    pass


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
    chunks = _chunk_text(transcript_text, max_chars=14000, overlap=800)
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
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You condense meeting transcript chunks. Preserve concrete decisions, "
                            "action items, owners, dates, risks, open questions, and important quotes. "
                            "Do not invent missing details."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Meeting title: {meeting_title or 'Untitled meeting'}\n"
                            f"Chunk {idx} of {len(chunks)}:\n\n{chunk}\n\n"
                            "Return compact bullet notes for this chunk only."
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
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a senior executive assistant creating accurate meeting minutes. "
                    "Use the user's handwritten notes as strong guidance, but verify against the transcript. "
                    "Never invent attendees, owners, due dates, or decisions. If something is unclear, say 'Not specified'. "
                    "Keep the output useful for business follow-up."
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

Create Markdown meeting minutes with exactly these sections:
# {meeting_title or 'Meeting Minutes'}
## Executive summary
## Key discussion points
## Decisions
## Action items
Use a Markdown table with columns: Owner, Action, Due date, Evidence.
## Risks and blockers
## Open questions
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
) -> str:
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "stream": False,
    }
    try:
        resp = requests.post(
            url,
            headers={"Authorization": "Bearer lm-studio", "Content-Type": "application/json"},
            json=payload,
            timeout=600,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()
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

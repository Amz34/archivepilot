"""AI answers over YOUR data — private-first.

Design: search results are turned into a prompt, sent to an
OpenAI-compatible endpoint (DeepSeek by default), and the model answers
using only the provided context. No API key? Local mode prints the
top matches instead — the archive still works fully offline.

Environment variables:
  AP_API_KEY      (or OPENAI_API_KEY / DEEPSEEK_API_KEY)
  AP_BASE_URL     default https://api.deepseek.com/v1
  AP_MODEL        default deepseek-chat
"""

import json
import os
import urllib.request

DEFAULT_BASE = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"


def _api_key() -> str | None:
    for var in ("AP_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"):
        val = os.environ.get(var)
        if val:
            return val
    return None


def _chat(messages: list[dict], timeout: int = 60) -> str:
    key = _api_key()
    if not key:
        raise RuntimeError("no API key configured")
    base = os.environ.get("AP_BASE_URL", DEFAULT_BASE).rstrip("/")
    model = os.environ.get("AP_MODEL", DEFAULT_MODEL)
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps({
            "model": model,
            "messages": messages,
            "temperature": 0.2,
            "stream": False,
        }).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode())
    return data["choices"][0]["message"]["content"]


def ask(archive, question: str, limit: int = 8) -> str:
    """Answer a question using only the user's own archived data."""
    hits = archive.search(question, limit=limit)
    if not hits:
        return "No relevant entries found in your archive for that question."

    context = "\n\n".join(
        f"[{h['source']} | {h['created_at']}]\n{h['raw']}" for h in hits
    )

    if not _api_key():
        # Local mode: give the evidence, no model call.
        return (
            "Local mode (no API key): here are the most relevant entries.\n"
            "Set AP_API_KEY to get AI-written answers.\n\n" + context
        )

    system = (
        "You answer questions using ONLY the user's personal archive "
        "entries provided as context. Cite the source of each fact. "
        "If the archive doesn't contain the answer, say so plainly. "
        "Answer in the language of the question (Arabic questions get "
        "Arabic answers)."
    )
    try:
        return _chat([
            {"role": "system", "content": system},
            {"role": "user",
             "content": f"Archive context:\n{context}\n\nQuestion: {question}"},
        ])
    except Exception as exc:  # network/auth trouble -> degrade gracefully
        return f"AI call failed ({exc}). Here are the closest matches:\n\n{context}"

"""Reads documents with Google Gemini, and with Groq when Gemini fails (busy, limit reached, no reply).

The result carries "_reader" ("Gemini" or "Groq") so the screens can say which one read it.
"""
from extractors import MissingApiKey
from extractors import gemini, groq


def extract(path, kind="job_card"):
    try:
        return {**gemini.extract(path, kind), "_reader": "Gemini"}
    except MissingApiKey:
        if not groq.has_key():
            raise           # ask for the Gemini key first
        gemini_problem = "Gemini has no API key yet."
    except Exception as exc:
        gemini_problem = str(exc)
    if not groq.has_key():
        raise MissingApiKey(groq.KEY_FILE, service="Groq", url=groq.KEY_URL,
                            reason=f"Gemini could not read the document:\n{gemini_problem[:400]}\n\n"
                                   "Add a free Groq key to use Groq as the backup reader.")
    try:
        return {**groq.extract(path, kind), "_reader": "Groq"}
    except MissingApiKey:
        raise
    except Exception as exc:
        raise RuntimeError(f"{gemini_problem}\n\n{exc}") from None

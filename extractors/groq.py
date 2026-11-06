"""Reads scanned documents with Groq (free tier), the backup when Gemini is busy or down.

Uses the same questions as the Gemini reader (extractors/gemini.py PROMPTS). Groq's vision models take
images only, so PDF pages are turned into JPEGs first (at most "groq_max_images" per document).
Needs a free API key from https://console.groq.com/keys, taken from the GROQ_API_KEY environment
variable or groq_api_key.txt in the tool folder. Models are tried in the order of config.json
"groq_models".
"""
import base64
import io
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image

from extractors import DOC_FIELDS, MissingApiKey
from extractors.gemini import PROMPTS

BASE_DIR = Path(__file__).resolve().parent.parent
KEY_FILE = BASE_DIR / "groq_api_key.txt"
KEY_URL = "https://console.groq.com/keys"
API_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MODELS = ["qwen/qwen3.8-27b"]
MAX_SIDE = 2000                 # pixels; keeps each image well inside Groq's request size limit
MAX_IMAGE_BYTES = 3_500_000
RETRY_DELAY_S = 3


def has_key():
    return bool(os.environ.get("GROQ_API_KEY", "").strip() or (KEY_FILE.exists() and KEY_FILE.read_text(
        encoding="utf-8").strip()))


def _api_key():
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if not key and KEY_FILE.exists():
        key = KEY_FILE.read_text(encoding="utf-8").strip()
    if not key:
        raise MissingApiKey(KEY_FILE, service="Groq", url=KEY_URL)
    return key


def _config():
    try:
        return json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _pages(path, limit):
    """PIL images for a file: every page of a PDF (up to `limit`), or the image itself."""
    path = Path(path)
    if path.suffix.lower() == ".pdf":
        pdf = pdfium.PdfDocument(str(path))
        return [pdf[i].render(scale=2).to_pil() for i in range(min(len(pdf), limit))]
    return [Image.open(path)]


def _data_url(img):
    img = img.convert("RGB")
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    quality = 85
    while True:
        buffer = io.BytesIO()
        img.save(buffer, "JPEG", quality=quality)
        if buffer.tell() <= MAX_IMAGE_BYTES or quality <= 40:
            return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()
        quality -= 15


def _images(paths, limit):
    images = []
    for path in paths:
        images += _pages(path, limit - len(images))
        if len(images) >= limit:
            break
    return [_data_url(img) for img in images[:limit]]


def _parse(text):
    """The JSON object in the reply (some models wrap it in thinking text or ``` fences)."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON in reply")
    return json.loads(text[start:end + 1])


class _Busy(Exception):
    pass


class _Skip(Exception):
    pass


def _call(model, key, body):
    request = urllib.request.Request(
        API_URL, data=json.dumps({**body, "model": model}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}",
                 "User-Agent": "odisha-tool/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            reply = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:400]
        if exc.code == 401:
            raise RuntimeError(f"Groq rejected the API key. Check {KEY_FILE.name}.\n\n{detail}") from None
        if exc.code in (429, 500, 502, 503, 504):
            raise _Busy(f"Groq {model}: {'free-tier limit reached' if exc.code == 429 else 'busy'} "
                        f"({exc.code})") from None
        if exc.code == 404 or "model" in detail.lower() and ("not exist" in detail.lower()
                                                            or "decommission" in detail.lower()):
            raise _Skip(f"Groq {model}: not available") from None
        raise _Skip(f"Groq {model}: error {exc.code}: {detail}") from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise _Busy(f"Groq {model}: no response ({getattr(exc, 'reason', exc)})") from None
    try:
        return _parse(reply["choices"][0]["message"]["content"])
    except (KeyError, IndexError, ValueError):
        raise _Busy(f"Groq {model}: returned no usable data") from None


def extract(path, kind="job_card"):
    fields = DOC_FIELDS[kind]
    config = _config()
    paths = path if isinstance(path, (list, tuple)) else [path]
    key = _api_key()
    prompt = (PROMPTS[kind] + "\n\nReply with only a JSON object with exactly these keys (string values, "
              "\"\" when unknown): " + ", ".join(fields))
    body = {
        "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}] + [
            {"type": "image_url", "image_url": {"url": url}}
            for url in _images(paths, config.get("groq_max_images", 3))]}],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    problems = []
    for model in config.get("groq_models") or DEFAULT_MODELS:
        for attempt in range(2):
            try:
                return _call(model, key, body)
            except _Skip as exc:
                problems.append(str(exc))
                break
            except _Busy as exc:
                problems.append(str(exc))
                if attempt == 0:
                    time.sleep(RETRY_DELAY_S)
    raise RuntimeError("Groq could not read the document either:\n- " + "\n- ".join(problems))

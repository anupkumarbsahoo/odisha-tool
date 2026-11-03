"""Reads scanned documents with Google Gemini: the handwritten job card ("Certificate
of Employment Proof by Self"), Aadhaar cards, bank passbooks, the employment exchange
"Sample Format (X-I)" card and qualification certificates / marksheets.

Needs a free API key from https://aistudio.google.com/apikey, taken from the
GEMINI_API_KEY environment variable or gemini_api_key.txt in the tool folder.
Models are tried in the order of config.json "gemini_models"; a busy or rate-limited
model is retried once, then the next one is used.
"""
import base64
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from extractors import DOC_FIELDS, MissingApiKey

BASE_DIR = Path(__file__).resolve().parent.parent
KEY_FILE = BASE_DIR / "gemini_api_key.txt"
API_ROOT = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-flash-latest"
RETRIES_PER_MODEL = 1
RETRY_DELAY_S = 3
MIME_TYPES = {".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
              ".png": "image/png", ".webp": "image/webp"}

JOB_CARD_PROMPT = """This is a scanned, handwritten Odisha "CERTIFICATE OF EMPLOYMENT PROOF BY SELF (Worker)" form.
Read the handwriting carefully and return the values below. Use "" for anything blank or unreadable; never guess.

- name: worker name after "This is to certify that I Sri/Smt."
- guardian_name: name after "S/D/W of"
- mobile: worker mobile number (often written at the very top of the page), digits only
- village: after "Village/Ward"
- post_office: after "Po"
- police_station: after "Ps"
- gp_municipality: after "G.P/NAC/Municipality"
- block: after "Block"
- district: after "Dist"
- pin: 6-digit PIN code (may appear in the employer column as "PIN-")
- nature_of_job: after "Nature of Job"
- employer_name: the employer's name in the "Name, Address of the Employer" column (may continue on the next row)
- employer_contact: the "Contact/Mob No" column, digits only
- employer_address: the employer's address lines from that column joined with ", " (e.g. "AT-NUAPADA, PO-MAHADEIJODA, PS-RAISUAN, BLOCK-KENDUJHAR, DIST-KENDUJHAR"), without the PIN
- employment_from: "Date from" of the first work row, as DD-MM-YYYY
- employment_to: "Date upto" of the first work row, as DD-MM-YYYY
- total_days: "Total Days Employed" of the first work row, digits only
From the employer's address lines in the "Name, Address of the Employer" column only (leave "" if that column does not give it):
- est_village: the village after "AT"
- est_post_office: the post office after "PO"
- est_block: the block after "BLOCK"
- est_district: the district after "DIST"
- est_pin: the 6-digit PIN after "PIN"
Write names and places in CAPITAL letters exactly as written."""

AADHAAR_PROMPT = """This is an Indian Aadhaar card (front and/or back side, possibly a PDF of both).
Return the values below. Use "" for anything not printed or unreadable; never guess.

- name: the holder's name in English
- dob: date of birth as DD-MM-YYYY (if only a year is printed, return just the year)
- sex: MALE or FEMALE
- aadhaar_number: the 12-digit Aadhaar number, digits only
- relation_type: "S/O", "D/O", "W/O" or "C/O" exactly as printed before a relative's name in the address, else ""
- relative_name: the name after that S/O, D/O, W/O or C/O
- address: the English address without the relative's name, district, state and PIN (e.g. "AMRUT VIHAR, PO KENDUJHARGARH")
- vtc: village / town / city (VTC)
- post_office: post office (PO)
- sub_district: sub district / tehsil / block if printed
- district: district
- state: state
- pin: 6-digit PIN code
Write names and places in CAPITAL letters."""

BANK_PROMPT = """This is an Indian bank passbook front page, cheque or bank account document.
Return the values below. Use "" for anything not printed or unreadable; never guess.

- account_holder: account holder's name
- bank_name: full bank name (e.g. "CENTRAL BANK OF INDIA")
- branch: branch name
- ifsc: 11-character IFSC code
- account_number: account number, digits only
Write names in CAPITAL letters."""

EMP_CARD_PROMPT = """This is a scanned, handwritten Odisha employment exchange "SAMPLE FORMAT (X-I)" card (a numbered table).
Read the handwriting carefully and return the values below. Use "" for anything blank or unreadable; never guess.

- name: 1. Applicant Name
- father_name: 2. Father's Name
- aadhaar: 3. Aadhar Number, digits only
- mother_name: 4. Mother's Name
- dob: 5. Date of Birth as DD-MM-YYYY
- caste: 6. Caste, only the category: GENERAL, OBC, SEBC, SC or ST
- sub_caste: the sub-caste written in brackets after the caste (e.g. "KUDMI"), else ""
From 7. Present Address:
- addr_at: after "At"
- addr_po: after "Po"
- tahasil: after "Tahasil"
- police_station: after "PS"
- district: after "Dist"
- pin: the 6-digit PIN
- res_cert_no: 9. Residential Certificate "No." (e.g. "E-RES/2025/1412254")
- res_cert_date: 9. Residential Certificate "Date" as DD-MM-YYYY
- caste_cert_no: 10. Caste Certificate "No." (e.g. "E-SEB/2025/304282")
- caste_cert_date: 10. Caste Certificate "Date" as DD-MM-YYYY
- mobile: 11. Mobile Number, digits only
- email: the "Email ID" written next to the mobile number, in lower case
From 12.:
- eye_sight: Eye sight
- height: Height exactly as written (e.g. "5.4" means 5 feet 4 inches)
- weight: Weight in kg, digits only
- chest: Chest
- blood_group: Blood Group (e.g. "B+")
Write names and places in CAPITAL letters exactly as written."""

CERT_PROMPT = """These pages are one Indian school/college qualification: its pass certificate and/or its marksheet.
Return the values below. Use "" for anything not printed or unreadable; never guess.

- level: one of 10TH PASS, 12TH PASS, ITI CERTIFICATE HOLDER, DIPLOMA HOLDER, GRADUATE, GRADUATE(TECH.),
  HONOURS GRADUATE, POST GRADUATE, DOCTORATE (High School Certificate / Matric = 10TH PASS; Higher Secondary /
  +2 / Intermediate = 12TH PASS)
- exam: the exam passed, e.g. "10TH/MATRIC PASS", "12TH/INTERMEDIATE SCIENCE", "12TH/INTERMEDIATE ARTS",
  "12TH/INTERMEDIATE COMMERCE", "B.A.", "B.SC.", "B.COM.", "M.A."
- board: the board or university in the style "BOARD OF SECONDARY EDUCATION,CUTTACK,ODISHA" (Odisha BSE) or
  "COUNCIL OF HIGHER SECONDARY EDUCATION, ODISHA" (Odisha CHSE); for others the full name in capitals
- school: the school / college / institute name with its place, e.g. "N K HIGH SCHOOL,BARHATIPURA"
- medium: the medium of instruction if printed (ODIA, ENGLISH or HINDI), else ""
- year: passing year, 4 digits
- month: the month the result was published (date of publication of result or certificate date), as a
  3-letter month like "Jul" (not the month the exam was held)
- cert_no: the certificate serial number if printed, else ""
- division: FIRST, SECOND or THIRD if a division is printed, else ""
- percentage: total marks obtained / total maximum marks x 100, rounded to a whole number (digits only)
- grade: the overall grade if printed (e.g. "B2"), else ""
- subjects: the subject names, comma separated, in capitals (e.g. "ODIA, ENGLISH, MATHEMATICS")"""

PROMPTS = {"job_card": JOB_CARD_PROMPT, "aadhaar": AADHAAR_PROMPT, "bank": BANK_PROMPT,
           "emp_card": EMP_CARD_PROMPT, "certificate": CERT_PROMPT}


def _api_key():
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key and KEY_FILE.exists():
        key = KEY_FILE.read_text(encoding="utf-8").strip()
    if not key:
        raise MissingApiKey(KEY_FILE)
    return key


def _models():
    """Models to try in order; later ones are fallbacks for when Google reports one as busy."""
    try:
        config = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        config = {}
    models = config.get("gemini_models") or [config.get("gemini_model") or DEFAULT_MODEL]
    return list(dict.fromkeys(models))


def _request(url, key, body=None):
    request = urllib.request.Request(
        url, data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", "x-goog-api-key": key})
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read())


def _available_models(key):
    try:
        models = _request(f"{API_ROOT}/models?pageSize=200", key).get("models", [])
        return [m["name"].removeprefix("models/") for m in models
                if "generateContent" in m.get("supportedGenerationMethods", []) and "flash" in m["name"]]
    except Exception:
        return []


def _call(model, key, body):
    """One generateContent call. Returns parsed fields, or raises _Busy / _Skip / RuntimeError."""
    try:
        reply = _request(f"{API_ROOT}/models/{model}:generateContent", key, body)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:400]
        if exc.code in (400, 401, 403) and "API_KEY" in detail.upper():
            raise RuntimeError(f"Gemini rejected the API key. Check {KEY_FILE.name}.\n\n{detail}") from None
        if exc.code in (429, 500, 502, 503, 504):
            raise _Busy(f"{model}: {'free-tier limit reached' if exc.code == 429 else 'busy'} ({exc.code})") from None
        if exc.code == 404:
            raise _Skip(f"{model}: not available") from None
        raise RuntimeError(f"Gemini error {exc.code}: {detail}") from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise _Busy(f"{model}: no response ({getattr(exc, 'reason', exc)})") from None
    try:
        return json.loads(reply["candidates"][0]["content"]["parts"][0]["text"])
    except (KeyError, IndexError, ValueError):
        raise _Busy(f"{model}: returned no usable data") from None


class _Busy(Exception):
    pass


class _Skip(Exception):
    pass


def extract(path, kind="job_card"):
    fields = DOC_FIELDS[kind]
    parts = []
    for one in path if isinstance(path, (list, tuple)) else [path]:
        one = Path(one)
        mime = MIME_TYPES.get(one.suffix.lower())
        if not mime:
            raise ValueError(f"Unsupported file type {one.suffix}; use PDF, JPG or PNG.")
        parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(one.read_bytes()).decode()}})
    key = _api_key()
    body = {
        "contents": [{"parts": parts + [{"text": PROMPTS[kind]}]}],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
            "responseSchema": {"type": "OBJECT",
                               "properties": {k: {"type": "STRING"} for k in fields},
                               "required": list(fields)},
        },
    }
    problems = []
    for model in _models():
        for attempt in range(RETRIES_PER_MODEL + 1):
            try:
                return _call(model, key, body)
            except _Skip as exc:
                problems.append(str(exc))
                break
            except _Busy as exc:
                problems.append(str(exc))
                if attempt < RETRIES_PER_MODEL:
                    time.sleep(RETRY_DELAY_S)
    raise RuntimeError("Google Gemini could not read the document right now:\n- " + "\n- ".join(problems) +
                       "\n\nThis is on Google's side. Wait a minute and click 'Read document' again, "
                       "or type the fields in by hand.")

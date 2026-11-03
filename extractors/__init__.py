"""Swappable document extractors.

An extractor is any module exposing ``extract(path, kind: str) -> dict`` (path: a file or a list of
files) that
returns values for the keys in ``DOC_FIELDS[kind]`` (missing keys are treated as
blank). Pick the module with the ``extractor`` key in config.json, e.g.
``"extractors.manual"``.
"""
import importlib

# Job card / "Certificate of Employment Proof by Self": session key -> label shown in the UI
FIELDS = {
    "name": "Worker Name",
    "guardian_name": "S/D/W of",
    "mobile": "Worker Mobile",
    "village": "Village / Ward",
    "post_office": "Post Office",
    "police_station": "Police Station",
    "gp_municipality": "G.P / NAC / Municipality",
    "block": "Block",
    "district": "District",
    "pin": "PIN",
    "nature_of_job": "Nature of Job",
    "employer_name": "Employer Name",
    "employer_contact": "Employer Contact",
    "employer_address": "Employer Address",
    "employment_from": "Employed From",
    "employment_to": "Employed Upto",
    "total_days": "Total Days",
}

# Establishment location from the job card's employer column (used by One Time Registration only)
JOB_CARD_EXTRA = {
    "est_village": "Establishment Village (AT)",
    "est_post_office": "Establishment PO",
    "est_block": "Establishment Block",
    "est_district": "Establishment District",
    "est_pin": "Establishment PIN",
}

AADHAAR_FIELDS = {
    "name": "Name",
    "dob": "Date of Birth",
    "sex": "Sex",
    "aadhaar_number": "Aadhaar Number",
    "relation_type": "Relation (S/O, D/O, W/O, C/O)",
    "relative_name": "Relative's Name",
    "address": "Address",
    "vtc": "Village / Town",
    "post_office": "Post Office",
    "sub_district": "Sub District",
    "district": "District",
    "state": "State",
    "pin": "PIN",
}

BANK_FIELDS = {
    "account_holder": "Account Holder",
    "bank_name": "Bank Name",
    "branch": "Branch",
    "ifsc": "IFSC",
    "account_number": "Account Number",
}

# Employment Registration: the handwritten "SAMPLE FORMAT (X-I)" card
EMP_CARD_FIELDS = {
    "name": "Applicant Name",
    "father_name": "Father's Name",
    "aadhaar": "Aadhaar Number",
    "mother_name": "Mother's Name",
    "dob": "Date of Birth",
    "caste": "Caste",
    "sub_caste": "Sub-Caste",
    "addr_at": "At (village)",
    "addr_po": "Po",
    "tahasil": "Tahasil",
    "police_station": "Police Station",
    "district": "District",
    "pin": "PIN",
    "res_cert_no": "Residential Certificate No",
    "res_cert_date": "Residential Certificate Date",
    "caste_cert_no": "Caste Certificate No",
    "caste_cert_date": "Caste Certificate Date",
    "mobile": "Mobile",
    "email": "Email",
    "eye_sight": "Eye Sight",
    "height": "Height",
    "weight": "Weight",
    "chest": "Chest",
    "blood_group": "Blood Group",
}

# Employment Registration: one qualification read from its certificate and/or marksheet
CERT_FIELDS = {
    "level": "Qualification Level",
    "exam": "Exam Passed",
    "board": "Board / University",
    "school": "School / Institute",
    "medium": "Medium of Instruction",
    "year": "Passing Year",
    "month": "Passing Month",
    "cert_no": "Certificate Number",
    "division": "Division",
    "percentage": "Percentage",
    "grade": "Grade",
    "subjects": "Subjects",
}

DOC_FIELDS = {"job_card": {**FIELDS, **JOB_CARD_EXTRA}, "aadhaar": AADHAAR_FIELDS, "bank": BANK_FIELDS,
              "emp_card": EMP_CARD_FIELDS, "certificate": CERT_FIELDS}


class MissingApiKey(Exception):
    """Raised by an extractor that needs an API key; key_file is where the UI should save it."""

    def __init__(self, key_file):
        super().__init__(f"No API key found in {key_file}")
        self.key_file = key_file


def load_extractor(module_name):
    module = importlib.import_module(module_name)
    if not callable(getattr(module, "extract", None)):
        raise ImportError(f"{module_name} has no extract(path, kind) function")
    return module


def run_extractor(module_name, path, kind="job_card"):
    """``path`` may also be a list of files that together make one document (certificate + marksheet)."""
    raw = load_extractor(module_name).extract(path, kind) or {}
    return {key: str(raw.get(key) or "").strip() for key in DOC_FIELDS[kind]}

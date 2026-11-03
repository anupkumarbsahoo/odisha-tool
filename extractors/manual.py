"""Default extractor: reads nothing from the scan.

The UI shows the scan next to editable fields and the operator types the
values. To automate this, add a module (e.g. extractors/ocr.py) with the same
``extract(path, kind) -> dict`` signature and point config.json "extractor" at it.
"""


def extract(path, kind="job_card"):
    return {}

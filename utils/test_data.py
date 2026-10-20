"""
Loads negative test cases from data/negative/*.json.

Each case is a dict with:
    case      short name, shown in the test report: test_x[email-double-at]
    why       (optional) why the case matters, for whoever reads the data file
    body      (optional) the exact request body, used as-is (can be {}, null, a list, a string)
    raw_body  (optional) text sent exactly as written, for bodies that aren't valid JSON to most clients
    remove    (optional) fields to delete from a valid payload
    override  (optional) fields to replace in a valid payload
    expect    {"status": 400, "code": "VALIDATION_ERROR", "fields": [...]}
"""
import json
from pathlib import Path

import pytest

NEGATIVE_DATA = Path(__file__).parent.parent / "data" / "negative"


def load_cases(filename: str) -> list:
    """One pytest.param per case, named after its "case" field."""
    cases = json.loads((NEGATIVE_DATA / filename).read_text())
    return [pytest.param(case, id=case["case"]) for case in cases]


def build_body(case: dict, valid: dict):
    """The request body for a case: its fixed body, or a valid payload with some fields removed or replaced."""
    if "body" in case:
        return case["body"]
    body = {k: v for k, v in valid.items() if k not in case.get("remove", [])}
    return body | case.get("override", {})

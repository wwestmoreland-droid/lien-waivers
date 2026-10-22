from decimal import Decimal

import pytest

from app.matching.normalize import (
    clean_invoice_no,
    looks_like_retention_invoice,
    normalize_vendor,
    retainage_pct_from_terms,
    split_vendor_project,
    state_from_project_number,
)


@pytest.mark.parametrize("a,b", [
    ("Palatine Builders Supply, inc.", "Palatine Builders Supply, Inc."),
    ("Palatine Builders Supply, Inc.", "Palatine Builders Supply"),
    ("R & S Services, LLC.", "R and S Services LLC"),
    ("Royal Textile Products’", "Royal Textile Products"),
    ("KC Demo, Inc", "KC DEMO INC."),
    ("Acme Co.", "Acme"),
])
def test_normalize_variants_match(a, b):
    assert normalize_vendor(a) == normalize_vendor(b)


def test_normalize_keeps_real_differences():
    assert normalize_vendor("Metro Fire & Security (Fire Alarm)") != normalize_vendor("Metro Fire & Security (Sprinkler)")


def test_split_handles_hyphen_in_vendor():
    raw = "Tri-State Glass - Mirror, Inc. - Studio Co - Palatine, IL"
    assert split_vendor_project(raw, "Studio Co - Palatine, IL") == "Tri-State Glass - Mirror, Inc."


def test_split_without_project_suffix_returns_whole_string():
    assert split_vendor_project("BP Environmental Services", "Studio Co - Palatine, IL") == "BP Environmental Services"


def test_split_tolerates_project_name_typo():
    raw = "Some Vendor - CCDPC - Henderson, NV"
    assert split_vendor_project(raw, "CCPDC - Henderson, NV") == "Some Vendor"


def test_state_from_project_number():
    assert state_from_project_number("P--CA-012425-1") == "CA"


def test_invoice_number_floats_cleaned():
    assert clean_invoice_no(134822.0) == "134822"
    assert clean_invoice_no("377841.0") == "377841"
    assert clean_invoice_no("Pay App 1") == "Pay App 1"


def test_retention_terms():
    assert retainage_pct_from_terms("Net 30 with 10% Retention") == Decimal("10")
    assert retainage_pct_from_terms("Net 30") is None
    assert looks_like_retention_invoice("Retention release", None)
    assert not looks_like_retention_invoice("Pay App 3", None)

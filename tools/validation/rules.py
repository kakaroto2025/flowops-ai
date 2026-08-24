from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from tools.documents.normalization import REGION_BR, REGION_UNKNOWN, REGION_US, business_key, normalize_tax_id


REQUIRED_FIELDS = ("company_name", "invoice_number", "issue_date", "total_amount")


def _only_digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def validate_cnpj(cnpj: str | None) -> bool:
    digits = _only_digits(cnpj)
    return len(digits) == 14 and len(set(digits)) > 1


def validate_ein(ein: str | None) -> bool:
    digits = _only_digits(ein)
    return len(digits) == 9 and len(set(digits)) > 1


def validate_date(value: str | None) -> bool:
    if not value:
        return False
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            datetime.strptime(value, fmt)
            return True
        except ValueError:
            pass
    return False


def validate_invoice_number(value: str | None) -> bool:
    if not value:
        return False
    normalized = re.sub(r"[^A-Z0-9]+", "", value.upper())
    if normalized in {"TA", "NF", "NFE", "NOTA", "NOTAFISCAL", "INVOICE", "NUMERO", "N"}:
        return False
    return bool(re.search(r"\d", value))


def validate_extraction(extraction: dict[str, Any], known_invoices: set[str] | set[tuple[str, str]] | None = None) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = list(extraction.get("warnings") or [])
    known_invoices = known_invoices or set()
    country_code = extraction.get("country_code")
    tax_id = extraction.get("tax_id") or extraction.get("cnpj")
    tax_id_type = extraction.get("tax_id_type")

    if not country_code and extraction.get("cnpj"):
        country_code = REGION_BR
    if country_code not in {REGION_BR, REGION_US}:
        errors.append("unknown_country")

    for field in REQUIRED_FIELDS:
        if extraction.get(field) in (None, ""):
            errors.append(f"missing_{field}")

    if not tax_id:
        errors.append("missing_tax_id")
        if country_code == REGION_BR or tax_id_type == "CNPJ":
            errors.append("missing_cnpj")
    elif country_code == REGION_BR or tax_id_type == "CNPJ":
        if not validate_cnpj(tax_id):
            errors.append("invalid_tax_id")
            errors.append("invalid_cnpj")
    elif country_code == REGION_US or tax_id_type == "EIN":
        if not validate_ein(tax_id):
            errors.append("invalid_tax_id")

    if extraction.get("issue_date") and not validate_date(extraction.get("issue_date")):
        errors.append("invalid_issue_date")

    if extraction.get("invoice_number") and not validate_invoice_number(extraction.get("invoice_number")):
        errors.append("invalid_invoice_number")

    amount = extraction.get("total_amount")
    if amount is not None:
        if amount <= 0:
            errors.append("invalid_total_amount")
        elif amount > 100_000:
            warnings.append("amount_anomaly")

    current_key = business_key(extraction)
    legacy_key = (str(extraction.get("cnpj") or ""), str(extraction.get("invoice_number") or ""))
    if current_key and current_key in known_invoices:
        errors.append("duplicate_invoice")
    elif all(legacy_key) and legacy_key in known_invoices:
        errors.append("duplicate_invoice")

    confidence = float(extraction.get("confidence") or 0)
    if confidence < 0.55:
        errors.append("low_confidence")
    elif confidence < 0.80:
        warnings.append("medium_confidence")

    retry_recommended = bool({"amount_anomaly", "amount_anomaly_possible_ocr_shift"} & set(warnings))
    if "invalid_invoice_number" in errors:
        retry_recommended = True
    human_review_recommended = bool(errors) and not retry_recommended

    status = "PASS"
    if errors:
        status = "FAIL"
    elif warnings:
        status = "WARN"

    return {
        "status": status,
        "errors": sorted(set(errors)),
        "warnings": sorted(set(warnings)),
        "retry_recommended": retry_recommended,
        "human_review_recommended": human_review_recommended,
    }

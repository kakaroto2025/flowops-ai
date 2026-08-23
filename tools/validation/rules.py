from __future__ import annotations

import re
from datetime import datetime
from typing import Any


REQUIRED_FIELDS = ("cnpj", "company_name", "invoice_number", "issue_date", "total_amount")


def _only_digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def validate_cnpj(cnpj: str | None) -> bool:
    digits = _only_digits(cnpj)
    return len(digits) == 14 and len(set(digits)) > 1


def validate_date(value: str | None) -> bool:
    if not value:
        return False
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
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


def validate_extraction(extraction: dict[str, Any], known_invoices: set[tuple[str, str]] | None = None) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = list(extraction.get("warnings") or [])
    known_invoices = known_invoices or set()

    for field in REQUIRED_FIELDS:
        if extraction.get(field) in (None, ""):
            errors.append(f"missing_{field}")

    if extraction.get("cnpj") and not validate_cnpj(extraction.get("cnpj")):
        errors.append("invalid_cnpj")

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

    invoice_key = (str(extraction.get("cnpj") or ""), str(extraction.get("invoice_number") or ""))
    if all(invoice_key) and invoice_key in known_invoices:
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

from __future__ import annotations

import re
from datetime import datetime
from typing import Any


REGION_AUTO = "AUTO"
REGION_BR = "BR"
REGION_US = "US"
REGION_UNKNOWN = "UNKNOWN"


def normalize_region(value: str | None) -> str:
    normalized = str(value or REGION_AUTO).strip().upper().replace("-", "_")
    aliases = {
        "AUTO_DETECT": REGION_AUTO,
        "BRAZIL": REGION_BR,
        "BRASIL": REGION_BR,
        "UNITED_STATES": REGION_US,
        "UNITED STATES": REGION_US,
        "USA": REGION_US,
    }
    return aliases.get(normalized, normalized if normalized in {REGION_AUTO, REGION_BR, REGION_US} else REGION_AUTO)


def only_alnum(value: str | None) -> str:
    return "".join(char for char in str(value or "") if char.isalnum())


def only_digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def normalize_invoice_number(value: str | None) -> str | None:
    normalized = only_alnum(value).upper()
    return normalized or None


def normalize_tax_id(value: str | None) -> str | None:
    digits = only_digits(value)
    return digits or None


def normalize_amount(value: Any, country_code: str | None = None) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, int | float):
        return float(value)

    text = str(value).strip()
    text = re.sub(r"[^0-9,.\-]", "", text)
    if not text:
        return None

    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(".", "").replace(",", ".")

    try:
        return float(text)
    except ValueError:
        return None


def normalize_date(value: str | None, country_code: str | None = None) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    formats = ["%Y-%m-%d"]
    if country_code == REGION_US:
        formats.extend(["%m/%d/%Y", "%m-%d-%Y"])
    formats.extend(["%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y", "%m-%d-%Y"])
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return text


def detect_country(text: str, extracted: dict[str, Any] | None = None, processing_region: str = REGION_AUTO) -> dict[str, Any]:
    region = normalize_region(processing_region)
    if region in {REGION_BR, REGION_US}:
        return {"country_code": region, "confidence": 1.0, "method": "USER_SELECTED"}

    extracted = extracted or {}
    extracted_values = " ".join(str(value) for value in extracted.values() if value not in (None, ""))
    haystack = f"{text}\n{extracted_values}".upper()
    br_score = 0
    us_score = 0

    if re.search(r"\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}", haystack):
        br_score += 4
    if any(term in haystack for term in ["CNPJ", "NOTA FISCAL", "NF-E", "NFS-E", "RAZAO SOCIAL", "RAZÃO SOCIAL", "BRL", "R$"]):
        br_score += 2

    if re.search(r"\b\d{2}-\d{7}\b", haystack):
        us_score += 4
    if any(term in haystack for term in ["EIN", "EMPLOYER IDENTIFICATION NUMBER", "TAX ID", "INVOICE #", "INVOICE NUMBER", "USD"]):
        us_score += 2
    if "$" in haystack and "R$" not in haystack:
        us_score += 1

    if br_score >= us_score + 2 and br_score >= 2:
        return {"country_code": REGION_BR, "confidence": min(0.99, 0.55 + br_score * 0.08), "method": "LOCAL_RULES"}
    if us_score >= br_score + 2 and us_score >= 2:
        return {"country_code": REGION_US, "confidence": min(0.99, 0.55 + us_score * 0.08), "method": "LOCAL_RULES"}
    return {"country_code": REGION_UNKNOWN, "confidence": 0.0, "method": "LOCAL_RULES"}


def normalize_extraction_payload(
    payload: dict[str, Any],
    *,
    raw_text: str = "",
    processing_region: str = REGION_AUTO,
) -> dict[str, Any]:
    result = dict(payload)
    selected = normalize_region(processing_region)
    detected = detect_country(raw_text, result, selected)
    country_code = result.get("country_code") or detected["country_code"]
    if country_code not in {REGION_BR, REGION_US, REGION_UNKNOWN}:
        country_code = REGION_UNKNOWN

    tax_id = result.get("tax_id") or result.get("cnpj")
    tax_id_type = result.get("tax_id_type")
    if country_code == REGION_BR:
        tax_id_type = tax_id_type or "CNPJ"
        result["cnpj"] = result.get("cnpj") or tax_id
        result["currency"] = result.get("currency") or "BRL"
    elif country_code == REGION_US:
        tax_id_type = tax_id_type or "EIN"
        result["currency"] = result.get("currency") or "USD"

    result["country_code"] = country_code
    result["country_confidence"] = float(result.get("country_confidence") or detected["confidence"] or 0.0)
    result["country_detection_method"] = detected["method"]
    result["tax_id"] = tax_id
    result["tax_id_type"] = tax_id_type
    result["normalized_tax_id"] = normalize_tax_id(tax_id)
    result["normalized_invoice_number"] = normalize_invoice_number(result.get("invoice_number"))
    result["issue_date"] = normalize_date(result.get("issue_date"), country_code)
    result["total_amount"] = normalize_amount(result.get("total_amount"), country_code)
    result["document_type"] = result.get("document_type") or "INVOICE"
    result["confidence"] = float(result.get("confidence") or 0.0)
    result["warnings"] = list(result.get("warnings") or [])
    return result


def business_key(payload: Any) -> str | None:
    if isinstance(payload, dict):
        country_code = payload.get("country_code")
        tax_id = payload.get("normalized_tax_id")
        invoice = payload.get("normalized_invoice_number")
    else:
        country_code = getattr(payload, "country_code", None)
        tax_id = getattr(payload, "normalized_tax_id", None)
        invoice = getattr(payload, "normalized_invoice_number", None)
    if not tax_id:
        raw_tax = getattr(payload, "tax_id", None) if not isinstance(payload, dict) else payload.get("tax_id")
        raw_cnpj = getattr(payload, "cnpj", None) if not isinstance(payload, dict) else payload.get("cnpj")
        tax_id = normalize_tax_id(raw_tax or raw_cnpj)
    if not invoice:
        raw_invoice = getattr(payload, "invoice_number", None) if not isinstance(payload, dict) else payload.get("invoice_number")
        invoice = normalize_invoice_number(raw_invoice)
    if not country_code:
        raw_cnpj = getattr(payload, "cnpj", None) if not isinstance(payload, dict) else payload.get("cnpj")
        country_code = REGION_BR if raw_cnpj else REGION_UNKNOWN
    if country_code and tax_id and invoice:
        return f"{str(country_code).upper()}|{tax_id}|{invoice}"
    return None

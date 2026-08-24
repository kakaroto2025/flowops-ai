from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Any

from tools.documents.normalization import (
    REGION_BR,
    REGION_US,
    detect_country,
    normalize_amount,
    normalize_extraction_payload,
    normalize_region,
)


def _looks_like_pdf(raw: bytes) -> bool:
    return raw.startswith(b"%PDF")


def _extract_pdf_text(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    page_text: list[str] = []
    for index, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        if text.strip():
            page_text.append(f"[page {index}]\n{text}")
    return "\n\n".join(page_text)


def read_document_text(path: str | Path) -> str:
    document_path = Path(path)
    raw = document_path.read_bytes()
    if _looks_like_pdf(raw):
        try:
            text = _extract_pdf_text(document_path)
            if text.strip():
                return text
        except Exception:
            pass

    for encoding in ("utf-8", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


def _find(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
    return match.group(1).strip() if match else None


def _normalize_label(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", " ", ascii_text.lower()).strip()


def _clean_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("[page ")]


def _line_after_label(lines: list[str], labels: set[str], start: int = 0) -> str | None:
    normalized_lines = [_normalize_label(line) for line in lines]
    for index in range(start, len(lines)):
        if normalized_lines[index] in labels:
            for candidate in lines[index + 1 :]:
                if candidate.strip():
                    return candidate.strip()
    return None


def _find_emitente_bounds(lines: list[str]) -> tuple[int, int]:
    normalized_lines = [_normalize_label(line) for line in lines]
    start = 0
    end = len(lines)
    for index, line in enumerate(normalized_lines):
        if line == "emitente":
            start = index + 1
            break
    for index in range(start, len(lines)):
        if normalized_lines[index] in {"destinatario", "codigo", "descricao"}:
            end = index
            break
    return start, end


def _parse_amount(value: str | None) -> float | None:
    return normalize_amount(value)


def _extract_emitente_field(lines: list[str], labels: set[str]) -> str | None:
    start, end = _find_emitente_bounds(lines)
    return _line_after_label(lines[:end], labels, start=start)


def _extract_company_name(text: str, lines: list[str]) -> str | None:
    emitente_name = _extract_emitente_field(lines, {"razao social", "empresa", "company"})
    if emitente_name:
        return emitente_name
    explicit = _find(r"(?:Raz[aÃ£]o Social|Empresa|Company|Vendor):\s*(.+)", text)
    if explicit:
        return explicit
    for index, line in enumerate(lines):
        normalized = _normalize_label(line)
        if normalized in {"invoice", "tax invoice", "bill", "receipt"} and index > 0:
            candidate = lines[index - 1].strip()
            if candidate and not re.search(r"\d{2,}", candidate):
                return candidate
    return None


def _extract_cnpj(text: str, lines: list[str]) -> str | None:
    emitente_cnpj = _extract_emitente_field(lines, {"cnpj"})
    if emitente_cnpj and re.fullmatch(r"[0-9.\-/]+", emitente_cnpj):
        return emitente_cnpj
    return _find(r"CNPJ:?\s*([0-9]{2}\.?[0-9]{3}\.?[0-9]{3}/?[0-9]{4}-?[0-9]{2})", text)


def _extract_ein(text: str, lines: list[str]) -> str | None:
    from_label = _line_after_label(
        lines,
        {"ein", "tax id", "tax identification number", "employer identification number"},
    )
    if from_label:
        match = re.search(r"\b[0-9]{2}-[0-9]{7}\b", from_label)
        if match:
            return match.group(0)
    return _find(
        r"(?:EIN|Tax ID|Employer Identification Number|Tax Identification Number):?\s*([0-9]{2}-[0-9]{7})",
        text,
    )


def _extract_invoice_number(text: str, lines: list[str]) -> str | None:
    from_label = _line_after_label(
        lines,
        {"numero da nota", "n da nota", "no da nota", "nota", "nf", "invoice", "invoice number", "invoice no", "invoice #"},
    )
    if from_label:
        match = re.search(r"[A-Z0-9\-]+", from_label, flags=re.IGNORECASE)
        if match:
            return match.group(0)
    for pattern in (
        r"(?:N[ºo]\s*|No\.\s*)([A-Z0-9\-]+)",
        r"(?:NF-e TESTE|NOTA)\s*N[ºo]?\s*([A-Z0-9\-]+)",
        r"(?:Nota|NF|Invoice):\s*([A-Z0-9\-]+)",
    ):
        value = _find(pattern, text)
        if value:
            return value
    return None


def _is_invoice_number_candidate(value: str | None) -> bool:
    if not value:
        return False
    candidate = value.strip().strip(".:-#")
    normalized = _normalize_label(candidate).replace(" ", "")
    if normalized in {"ta", "nf", "nfe", "nota", "notafiscal", "invoice", "numero", "n"}:
        return False
    return bool(re.search(r"\d", candidate))


def _extract_invoice_number(text: str, lines: list[str]) -> str | None:
    from_label = _line_after_label(
        lines,
        {"numero da nota", "n da nota", "no da nota", "nota", "nf", "invoice number", "invoice no", "invoice #"},
    )
    if from_label:
        match = re.search(r"[A-Z0-9][A-Z0-9.\-]*", from_label, flags=re.IGNORECASE)
        if match and _is_invoice_number_candidate(match.group(0)):
            return match.group(0)

    for line in lines:
        for pattern in (
            r"^\s*(?:N[º°o]\.?)\s*([A-Z0-9][A-Z0-9.\-]*)\s*$",
            r"^\s*(?:NF-e|NFE|NOTA|NOTA FISCAL).*?(?:N[º°o]\.?)\s*([A-Z0-9][A-Z0-9.\-]*)\b",
            r"^\s*(?:Nota|NF|Invoice|Invoice Number|Invoice No\.?|Invoice #)\s*:\s*([A-Z0-9][A-Z0-9.\-]*)\s*$",
            r"^\s*(?:Invoice Number|Invoice No\.?|Invoice #)\s*([A-Z0-9][A-Z0-9.\-]*)\s*$",
        ):
            match = re.search(pattern, line, flags=re.IGNORECASE)
            if match and _is_invoice_number_candidate(match.group(1)):
                return match.group(1)

    return None


def _extract_issue_date(text: str, lines: list[str]) -> str | None:
    from_label = _line_after_label(lines, {"data de emissao", "data", "issue date", "invoice date"})
    if from_label:
        match = re.search(r"[0-9]{2}/[0-9]{2}/[0-9]{4}|[0-9]{2}-[0-9]{2}-[0-9]{4}|[0-9]{4}-[0-9]{2}-[0-9]{2}", from_label)
        if match:
            return match.group(0)
    return _find(
        r"(?:Data(?: de Emiss[aÃ£]o)?|Issue Date|Invoice Date):\s*([0-9]{2}/[0-9]{2}/[0-9]{4}|[0-9]{2}-[0-9]{2}-[0-9]{4}|[0-9]{4}-[0-9]{2}-[0-9]{2})",
        text,
    )


def _extract_total_amount(text: str, lines: list[str]) -> float | None:
    normalized_lines = [_normalize_label(line) for line in lines]
    for index, label in enumerate(normalized_lines):
        if label in {"valor total da nota", "total da nota"}:
            for candidate in lines[index + 1 :]:
                amount = _parse_amount(candidate)
                if amount is not None:
                    return amount

    matches = re.findall(r"R\$\s*[0-9]{1,3}(?:\.[0-9]{3})*,[0-9]{2}|R\$\s*[0-9]+(?:\.[0-9]+)?|US\$?\s*[0-9]{1,3}(?:,[0-9]{3})*\.[0-9]{2}|\$\s*[0-9]{1,3}(?:,[0-9]{3})*\.[0-9]{2}", text)
    parsed_amounts = [amount for amount in (_parse_amount(match) for match in matches) if amount is not None]
    if parsed_amounts:
        return parsed_amounts[-1]
    return _parse_amount(_find(r"(?:Valor Total|Total Amount|Amount Due|Total|Amount):\s*(?:USD\s*)?(R?\$?\s*[0-9.,]+)", text))


def extract_document(path: str | Path, retry_count: int = 0, processing_region: str = "AUTO") -> dict[str, Any]:
    text = read_document_text(path)
    lines = _clean_lines(text)
    warnings: list[str] = []
    region = normalize_region(processing_region)

    if "ILLEGIBLE" in text:
        return {
            "document_type": "INVOICE",
            "country_code": None if region == "AUTO" else region,
            "tax_id": None,
            "tax_id_type": None,
            "cnpj": None,
            "company_name": None,
            "invoice_number": None,
            "issue_date": None,
            "total_amount": None,
            "currency": None,
            "confidence": 0.18,
            "warnings": ["document_unreadable"],
        }

    country_hint = detect_country(text, processing_region=region)["country_code"]
    company_name = _find(r"(?:Raz[aã]o Social|Empresa|Company|Vendor):\s*(.+)", text)
    cnpj = _find(r"CNPJ:\s*([0-9.\-/]+)", text)
    ein = _extract_ein(text, lines)
    invoice_number = _find(r"(?:Nota|NF|Invoice|Invoice Number|Invoice No\.?|Invoice #):\s*([A-Z0-9\-]+)", text)
    issue_date = _find(r"(?:Data|Issue Date|Invoice Date):\s*([0-9]{2}/[0-9]{2}/[0-9]{4}|[0-9]{2}-[0-9]{2}-[0-9]{4}|[0-9]{4}-[0-9]{2}-[0-9]{2})", text)
    amount = _parse_amount(_find(r"(?:Valor Total|Total Amount|Amount Due|Total|Amount):\s*(?:USD\s*)?(R?\$?\s*[0-9.,]+)", text))

    company_name = _extract_company_name(text, lines) or company_name
    cnpj = _extract_cnpj(text, lines) or cnpj
    ein = _extract_ein(text, lines) or ein
    invoice_number = _extract_invoice_number(text, lines) or invoice_number
    issue_date = _extract_issue_date(text, lines) or issue_date
    amount = _extract_total_amount(text, lines) or amount

    if "AMBIGUOUS_AMOUNT" in text and retry_count == 0 and amount is not None:
        amount *= 10
        warnings.append("amount_anomaly_possible_ocr_shift")

    tax_id = cnpj if country_hint == REGION_BR else ein if country_hint == REGION_US else cnpj or ein
    tax_id_type = "CNPJ" if tax_id and tax_id == cnpj else "EIN" if tax_id and tax_id == ein else None
    currency = "BRL" if "R$" in text or country_hint == REGION_BR else "USD" if "$" in text or "USD" in text.upper() or country_hint == REGION_US else None
    payload = normalize_extraction_payload(
        {
            "document_type": "INVOICE",
            "country_code": None if country_hint == "UNKNOWN" else country_hint,
            "tax_id": tax_id,
            "tax_id_type": tax_id_type,
            "cnpj": cnpj,
            "company_name": company_name,
            "invoice_number": invoice_number,
            "issue_date": issue_date,
            "total_amount": amount,
            "currency": currency,
            "confidence": 0.96,
            "warnings": warnings,
        },
        raw_text=text,
        processing_region=region,
    )

    required_tax_field = "cnpj" if payload.get("country_code") == REGION_BR else "tax_id"
    missing = [
        field
        for field, value in {
            "company_name": company_name,
            required_tax_field: payload.get("tax_id"),
            "invoice_number": invoice_number,
            "issue_date": issue_date,
            "total_amount": amount,
        }.items()
        if value in (None, "")
    ]
    warnings.extend([f"missing_{field}" for field in missing])

    confidence = 0.96
    if missing:
        confidence -= 0.12 * len(missing)
    if warnings:
        confidence -= 0.18
    confidence = max(0.05, min(0.99, confidence))

    payload["confidence"] = round(confidence, 2)
    payload["warnings"] = warnings
    return payload

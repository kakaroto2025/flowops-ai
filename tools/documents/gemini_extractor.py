from __future__ import annotations

import json
import os
import re
import contextlib
import io
from pathlib import Path
from typing import Any

from google import genai
from google.genai import errors as genai_errors
from google.genai import types


ROOT = Path(__file__).resolve().parents[2]
MODEL = "gemini-3.6-flash"
EXPECTED_KEYS = (
    "country_code",
    "tax_id",
    "tax_id_type",
    "company_name",
    "invoice_number",
    "issue_date",
    "total_amount",
    "currency",
    "document_type",
    "confidence",
)


class GeminiExtractionError(RuntimeError):
    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.details = details or {}


def _safe_api_error_details(exc: Exception) -> dict[str, Any]:
    details: dict[str, Any] = {
        "error_type": type(exc).__name__,
    }
    response_json = exc.args[1] if len(getattr(exc, "args", ())) > 1 and isinstance(exc.args[1], dict) else None
    api_error = response_json.get("error", {}) if isinstance(response_json, dict) else {}

    http_status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if http_status is None and isinstance(api_error, dict):
        http_status = api_error.get("code")
    api_status = getattr(exc, "status", None)
    if not api_status and isinstance(api_error, dict):
        api_status = api_error.get("status")
    api_message = getattr(exc, "message", None)
    if not api_message and isinstance(api_error, dict):
        api_message = api_error.get("message")

    if http_status is not None:
        details["http_status"] = http_status
    if api_status:
        details["api_status"] = str(api_status)
    if api_message:
        details["api_message"] = str(api_message)[:500]

    return details


def load_env(path: Path | None = None) -> None:
    env_path = path or ROOT / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def build_prompt(fiscal_text: str, processing_region: str = "AUTO") -> str:
    return f"""
Extraia os dados fiscais do texto abaixo.

Responda somente com JSON valido, sem markdown e sem explicacoes.
Use exatamente estas chaves:
{{
  "country_code": "",
  "tax_id": "",
  "tax_id_type": "",
  "company_name": "",
  "invoice_number": "",
  "issue_date": "",
  "total_amount": 0.0,
  "currency": "",
  "document_type": "",
  "confidence": 0.0
}}

Regras:
- processing_region solicitado: {processing_region}
- country_code deve ser "BR", "US" ou null quando nao houver evidencia suficiente.
- Para Brasil, tax_id_type deve ser "CNPJ" e tax_id deve ser o CNPJ do emitente.
- Para Estados Unidos, tax_id_type deve ser "EIN" e tax_id deve ser o EIN/Tax ID do vendor.
- Preserve zeros a esquerda em invoice_number.
- Nunca invente campos ausentes.
- Use null quando um campo nao estiver presente.
- total_amount deve ser numero JSON.
- confidence deve ser numero JSON entre 0.0 e 1.0.
- company_name deve ser a empresa emissora/vendor, nao o destinatario/customer.
- currency deve ser BRL, USD ou null.
- document_type deve ser INVOICE ou null.

Texto fiscal:
{fiscal_text[:12000]}
""".strip()


def extract_json(raw_text: str) -> dict[str, Any]:
    text = raw_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            raise GeminiExtractionError("model_response_was_not_json") from None
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise GeminiExtractionError("model_response_json_parse_failed") from exc

    if not isinstance(payload, dict):
        raise GeminiExtractionError("model_response_json_was_not_object")

    if "cnpj" in payload and "tax_id" not in payload:
        payload["tax_id"] = payload.get("cnpj")
        payload["tax_id_type"] = payload.get("tax_id_type") or "CNPJ"
        payload["country_code"] = payload.get("country_code") or "BR"
        payload["currency"] = payload.get("currency") or "BRL"
        payload["document_type"] = payload.get("document_type") or "INVOICE"

    missing_keys = [key for key in EXPECTED_KEYS if key not in payload]
    if missing_keys:
        raise GeminiExtractionError("model_response_missing_expected_keys")

    return {key: payload.get(key) for key in EXPECTED_KEYS}


def normalize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)

    invoice_number = normalized.get("invoice_number")
    if invoice_number is not None:
        if not isinstance(invoice_number, str):
            raise GeminiExtractionError("model_response_invalid_invoice_number_type")
        normalized["invoice_number"] = str(invoice_number)

    for key in ("country_code", "tax_id", "tax_id_type", "company_name", "issue_date", "currency", "document_type"):
        if normalized.get(key) is not None:
            normalized[key] = str(normalized[key])

    if normalized.get("confidence") is None:
        normalized["confidence"] = 0.0

    for key in ("total_amount", "confidence"):
        if normalized.get(key) is not None:
            try:
                normalized[key] = float(normalized[key])
            except (TypeError, ValueError) as exc:
                raise GeminiExtractionError(f"model_response_invalid_{key}") from exc

    confidence = normalized.get("confidence")
    if confidence is not None and not 0.0 <= confidence <= 1.0:
        raise GeminiExtractionError("model_response_invalid_confidence")

    normalized["document_type"] = str(normalized.get("document_type") or "INVOICE").upper()
    if normalized.get("tax_id_type") == "CNPJ":
        normalized["cnpj"] = normalized.get("tax_id")
    else:
        normalized["cnpj"] = None
    normalized["warnings"] = [
        f"missing_{key}"
        for key, value in {
            "country_code": normalized.get("country_code"),
            "tax_id": normalized.get("tax_id"),
            "tax_id_type": normalized.get("tax_id_type"),
            "company_name": normalized.get("company_name"),
            "invoice_number": normalized.get("invoice_number"),
            "issue_date": normalized.get("issue_date"),
            "total_amount": normalized.get("total_amount"),
            "currency": normalized.get("currency"),
        }.items()
        if value in (None, "")
    ]
    return normalized


def extract_with_gemini(fiscal_text: str, *, model: str = MODEL, processing_region: str = "AUTO") -> dict[str, Any]:
    load_env()
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key or api_key == "SUA_CHAVE_REAL_AQUI":
        raise GeminiExtractionError("gemini_api_key_not_configured")

    try:
        with contextlib.redirect_stderr(io.StringIO()):
            client = genai.Client(api_key=api_key)
            chat = client.chats.create(model=model)
            response = chat.send_message(
                build_prompt(fiscal_text, processing_region),
                config=types.GenerateContentConfig(response_mime_type="application/json"),
            )
    except (genai_errors.ClientError, genai_errors.ServerError, genai_errors.APIError) as exc:
        details = _safe_api_error_details(exc)
        raise GeminiExtractionError(f"gemini_request_failed:{type(exc).__name__}", details) from exc
    except Exception as exc:
        details = {"error_type": type(exc).__name__}
        raise GeminiExtractionError(f"gemini_request_failed:{type(exc).__name__}", details) from exc

    response_text = getattr(response, "text", "") or ""
    if not response_text.strip():
        raise GeminiExtractionError("model_response_empty")

    return normalize_payload(extract_json(response_text))

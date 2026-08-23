from __future__ import annotations

import os
import sys
from pathlib import Path

from google import genai


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL = "gemini-3.6-flash"


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main() -> int:
    load_env(ROOT / ".env")
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key or api_key == "SUA_CHAVE_REAL_AQUI":
        print("ERROR: GEMINI_API_KEY not configured in .env")
        return 1

    model = os.environ.get("GEMINI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    try:
        client = genai.Client(api_key=api_key)
        chat = client.chats.create(model=model)
        response = chat.send_message("Responda somente OK")
    except Exception as exc:
        print(f"ERROR: Gemini request failed safely: {type(exc).__name__}: {exc}")
        return 1

    text = (getattr(response, "text", None) or "").strip()
    print(f"MODEL={model}")
    print(f"RESULT={text}")
    return 0 if text else 1


if __name__ == "__main__":
    raise SystemExit(main())

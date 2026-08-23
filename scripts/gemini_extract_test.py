from __future__ import annotations

import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.documents.gemini_extractor import MODEL, extract_with_gemini, load_env

FISCAL_TEXT = """
NOTA FISCAL FICTICIA - SOMENTE PARA TESTE DO FLOWOPS AI
NOTA FISCAL ELETRONICA - TESTE
N 000145781
Serie 001

EMITENTE
Razao Social
ORION TECNOLOGIA EMPRESARIAL LTDA
CNPJ
31.415.926/0001-71
Data de Emissao
14/08/2026
Numero da Nota
000145781

DESTINATARIO
Razao Social
NOVA ERA COMERCIO E SERVICOS LTDA
CNPJ destinatario
45.678.901/0001-55

Codigo
Descricao
Qtd.
Valor Unit.
Valor Total
TI-101
Estacao de trabalho corporativa
3
R$ 4.200,00
R$ 12.600,00
TI-205
Instalacao e configuracao
3
R$ 450,00
R$ 1.350,00
VALOR TOTAL DA NOTA
R$ 13.950,00
""".strip()


def main() -> int:
    load_env(ROOT / ".env")
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key or api_key == "SUA_CHAVE_REAL_AQUI":
        print("ERROR: GEMINI_API_KEY not configured in .env")
        return 1

    try:
        payload = {
            key: value
            for key, value in extract_with_gemini(FISCAL_TEXT, model=MODEL).items()
            if key not in {"document_type", "warnings"}
        }
    except Exception as exc:
        print(f"ERROR: Gemini extraction test failed safely: {type(exc).__name__}: {exc}")
        return 1

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

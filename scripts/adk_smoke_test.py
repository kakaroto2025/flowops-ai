from __future__ import annotations

import asyncio
import contextlib
import io
import os
from importlib.metadata import version
from pathlib import Path

from google.adk.agents import Agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types


ROOT = Path(__file__).resolve().parents[1]
MODEL = "gemini-3.6-flash"


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def configure_google_api_key() -> bool:
    load_env(ROOT / ".env")
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key or api_key == "SUA_CHAVE_REAL_AQUI":
        print("ERROR: GEMINI_API_KEY not configured in .env")
        return False

    os.environ["GOOGLE_API_KEY"] = api_key
    os.environ.pop("GEMINI_API_KEY", None)
    return True


async def run_smoke_test() -> str:
    agent = Agent(
        name="flowops_smoke_agent",
        model=MODEL,
        instruction="Responda exatamente OK.",
    )
    session_service = InMemorySessionService()
    app_name = "flowops_adk_smoke"
    user_id = "smoke_user"
    session_id = "smoke_session"
    await session_service.create_session(
        app_name=app_name,
        user_id=user_id,
        session_id=session_id,
    )
    runner = Runner(
        app_name=app_name,
        agent=agent,
        session_service=session_service,
    )
    message = types.Content(
        role="user",
        parts=[types.Part(text="Responda somente OK")],
    )

    texts: list[str] = []
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session_id,
        new_message=message,
    ):
        content = getattr(event, "content", None)
        if not content or not getattr(content, "parts", None):
            continue
        for part in content.parts:
            text = getattr(part, "text", None)
            if text:
                texts.append(text.strip())

    return "\n".join(texts).strip()


def main() -> int:
    if not configure_google_api_key():
        return 1

    try:
        with contextlib.redirect_stderr(io.StringIO()):
            result = asyncio.run(run_smoke_test())
    except Exception as exc:
        print(f"ERROR: ADK smoke test failed safely: {type(exc).__name__}: {exc}")
        return 1

    print(f"ADK_VERSION={version('google-adk')}")
    print(f"MODEL={MODEL}")
    print(f"RESULT={result}")
    return 0 if result == "OK" else 1


if __name__ == "__main__":
    raise SystemExit(main())

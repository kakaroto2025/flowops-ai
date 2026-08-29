from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class GeminiPricing:
    model: str
    input_price_per_million_tokens: float | None = None
    output_price_per_million_tokens: float | None = None

    @classmethod
    def from_env(cls, model: str) -> "GeminiPricing":
        return cls(
            model=model,
            input_price_per_million_tokens=_env_float("FLOWOPS_GEMINI_INPUT_PRICE_PER_MILLION_TOKENS"),
            output_price_per_million_tokens=_env_float("FLOWOPS_GEMINI_OUTPUT_PRICE_PER_MILLION_TOKENS"),
        )

    def estimate_usd(self, input_tokens: int | None, output_tokens: int | None) -> float | None:
        if (
            input_tokens is None
            or output_tokens is None
            or self.input_price_per_million_tokens is None
            or self.output_price_per_million_tokens is None
        ):
            return None
        return round(
            (input_tokens / 1_000_000 * self.input_price_per_million_tokens)
            + (output_tokens / 1_000_000 * self.output_price_per_million_tokens),
            8,
        )


def _env_float(name: str) -> float | None:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return None
    return float(raw)

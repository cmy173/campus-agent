"""Configurable local execution budgets, independent of provider latency."""
from __future__ import annotations

from dataclasses import dataclass
import os


def positive(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        raise ValueError(f'{name} must be a positive integer') from None
    if value <= 0:
        raise ValueError(f'{name} must be a positive integer')
    return value


@dataclass(frozen=True)
class RunLimits:
    model_calls: int = 128
    tool_calls: int = 512
    turn_seconds: int = 1800
    max_output_tokens: int = 16384

    @classmethod
    def from_env(cls):
        return cls(positive('CAMPUS_MAX_MODEL_CALLS', 128), positive('CAMPUS_MAX_TOOL_CALLS', 512),
                   positive('CAMPUS_TURN_TIMEOUT_SECONDS', 1800), positive('CAMPUS_MAX_OUTPUT_TOKENS', 16384))

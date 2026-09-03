from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _load_project_env() -> None:
    # Load a local .env if present. Never overrides variables already exported
    # by the shell.
    here = Path.cwd()
    for candidate in (here / ".env", here.parent / ".env"):
        if candidate.exists():
            load_dotenv(candidate, override=False)
            break


@dataclass(frozen=True)
class Settings:
    t212_env: str
    api_key: str
    api_secret: str
    live_trading_requested: bool

    @property
    def base_url(self) -> str:
        if self.t212_env == "demo":
            return "https://demo.trading212.com/api/v0"
        if self.t212_env == "live":
            return "https://live.trading212.com/api/v0"
        raise ValueError(f"Unsupported SP1_T212_ENV={self.t212_env!r}")

    @classmethod
    def from_env(cls) -> Settings:
        _load_project_env()
        env = os.getenv("SP1_T212_ENV", "demo").strip().lower()
        return cls(
            t212_env=env,
            api_key=os.getenv("SP1_T212_API_KEY", "").strip(),
            api_secret=os.getenv("SP1_T212_API_SECRET", "").strip(),
            live_trading_requested=os.getenv("SP1_LIVE_TRADING", "false").strip().lower()
            in {"1", "true", "yes"},
        )

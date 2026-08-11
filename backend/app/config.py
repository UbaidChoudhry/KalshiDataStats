from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class UnsafeDataDirectory(ValueError):
    """Raised when local data would be written into the public repository."""


@dataclass(frozen=True)
class Settings:
    repository_root: Path
    data_dir: Path
    requests_per_second: float
    base_url: str
    pause_seconds: int
    max_pauses: int
    sync_mode: str
    host: str
    port: int

    @classmethod
    def from_environment(cls, repository_root: Path | None = None) -> Settings:
        root = (repository_root or Path(__file__).resolve().parents[2]).resolve()
        # Local-only operational values may live here; explicit shell environment wins.
        load_dotenv(root / ".env", override=False)
        raw_data_dir = os.getenv("KALSHI_DATA_DIR")
        default = Path.home() / "Library" / "Application Support" / "KalshiDataStats"
        data_dir = Path(raw_data_dir).expanduser() if raw_data_dir else default
        data_dir = data_dir.resolve()
        try:
            data_dir.relative_to(root)
        except ValueError:
            pass
        else:
            raise UnsafeDataDirectory(
                "KALSHI_DATA_DIR must be outside the repository so data cannot be committed."
            )
        mode = os.getenv("KALSHI_SYNC_MODE", "real").lower()
        if mode not in {"demo", "real"}:
            raise ValueError("KALSHI_SYNC_MODE must be 'demo' or 'real'.")
        rate = float(os.getenv("KALSHI_REQUESTS_PER_SECOND", "5"))
        if rate <= 0:
            raise ValueError("KALSHI_REQUESTS_PER_SECOND must be greater than zero.")
        pause_seconds = int(os.getenv("KALSHI_429_PAUSE_SECONDS", "60"))
        max_pauses = int(os.getenv("KALSHI_429_MAX_PAUSES", "3"))
        if pause_seconds <= 0 or max_pauses <= 0:
            raise ValueError("KALSHI_429_PAUSE_SECONDS and KALSHI_429_MAX_PAUSES must be positive.")
        return cls(
            repository_root=root,
            data_dir=data_dir,
            requests_per_second=rate,
            base_url=os.getenv("KALSHI_BASE_URL", "https://external-api.kalshi.com/trade-api/v2").rstrip("/"),
            pause_seconds=pause_seconds,
            max_pauses=max_pauses,
            sync_mode=mode,
            host=os.getenv("KALSHI_HOST", "127.0.0.1"),
            port=int(os.getenv("KALSHI_PORT", "8000")),
        )

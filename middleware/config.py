"""Runtime configuration.

The only setting that changes between deployment modes is ``STRAIKER_DETECT_URL``:

* standalone demo   -> https://api.prod.straiker.ai/api/v1/detect  (public, bearer auth)
* bolt-on in front of argus -> http://argus:8000/api/v1/detect     (cluster-local)

Nothing else in the service is mode-aware, which is what keeps the eventual in-tree merge
a transport swap rather than a rewrite.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_DETECT_URL = "https://api.prod.straiker.ai/api/v1/detect"


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    return default if raw is None else raw.strip().lower() in ("1", "true", "yes", "on")


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


@dataclass(frozen=True, slots=True)
class Settings:
    detect_url: str
    api_key: str
    x_tool: str
    sign_payloads: bool
    chatter_filter: bool
    block_enabled: bool
    detect_timeout: float
    dedup_ttl: int
    default_user_name: str

    @staticmethod
    def from_env() -> "Settings":
        detect_url = os.getenv("STRAIKER_DETECT_URL", DEFAULT_DETECT_URL)
        if detect_url.rstrip("/").endswith("/webhook"):
            raise ValueError(
                "STRAIKER_DETECT_URL must be the /api/v1/detect coding-agent path, not the webhook path; "
                "the webhook path does not run the coding-agent pipeline"
            )
        return Settings(
            detect_url=detect_url,
            api_key=os.getenv("STRAIKER_API_KEY", ""),
            x_tool=os.getenv("STRAIKER_X_TOOL", "claude-code"),
            sign_payloads=_bool("STRAIKER_SIGN_PAYLOADS", True),
            chatter_filter=_bool("STRAIKER_CHATTER_FILTER", True),
            block_enabled=_bool("STRAIKER_BLOCK_ENABLED", True),
            detect_timeout=_float("STRAIKER_DETECT_TIMEOUT", 2.5),
            dedup_ttl=int(_float("STRAIKER_DEDUP_TTL", 3600)),
            default_user_name=os.getenv("STRAIKER_DEFAULT_USER_NAME", "portkey-coding"),
        )

"""Settings read from environment variables (optionally from a .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

STORES = ("safeway", "fred_meyer", "qfc", "metmarket", "grocery_outlet")
STORE_LABELS = {
    "safeway": "Safeway",
    "fred_meyer": "Fred Meyer",
    "qfc": "QFC",
    "metmarket": "Metropolitan Market",
    "grocery_outlet": "Grocery Outlet",
}

# Stores without an API: their weekly ad (URL or file) is read by Claude.
AD_SOURCE_VARS = {
    "safeway": "SAFEWAY_AD_SOURCE",
    "metmarket": "METMARKET_AD_SOURCE",
    "grocery_outlet": "GROCERY_OUTLET_AD_SOURCE",
}


def load_dotenv(path: str | Path = ".env") -> None:
    """Minimal .env loader: KEY=VALUE lines, existing env vars win."""
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _pairs(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in raw.split(","):
        if ":" in part:
            k, v = part.split(":", 1)
            if k.strip() and v.strip():
                out[k.strip()] = v.strip()
    return out


def _list(raw: str) -> list[str]:
    return [p.strip() for p in raw.split(",") if p.strip()]


@dataclass
class Settings:
    db_path: str = "data/grocery.db"
    out_dir: str = "out"
    kroger_client_id: str = ""
    kroger_client_secret: str = ""
    kroger_locations: dict[str, str] = field(default_factory=dict)
    kroger_extra_terms: list[str] = field(default_factory=list)
    ad_sources: dict[str, str] = field(default_factory=dict)
    llm_model: str = "claude-opus-5-5"
    gmail_user: str = ""
    gmail_app_password: str = ""
    digest_to: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        env = os.environ.get
        ad_sources = {store: env(var, "") for store, var in AD_SOURCE_VARS.items() if env(var)}
        return cls(
            db_path=env("GROCERY_DB", "data/grocery.db"),
            out_dir=env("GROCERY_OUT_DIR", "out"),
            kroger_client_id=env("KROGER_CLIENT_ID", ""),
            kroger_client_secret=env("KROGER_CLIENT_SECRET", ""),
            kroger_locations=_pairs(env("KROGER_LOCATIONS", "")),
            kroger_extra_terms=_list(env("KROGER_EXTRA_TERMS", "")),
            ad_sources=ad_sources,
            llm_model=env("GROCERY_LLM_MODEL", "claude-opus-5-5"),
            gmail_user=env("GMAIL_USER", ""),
            gmail_app_password=env("GMAIL_APP_PASSWORD", ""),
            digest_to=env("DIGEST_TO", "") or env("GMAIL_USER", ""),
        )

    def configured_stores(self) -> list[str]:
        """Stores we expect deals from each week (used for zero-deal alerts)."""
        stores = []
        if self.kroger_client_id:
            stores += [s for s in self.kroger_locations if s in STORES]
        stores += [s for s in self.ad_sources if s in STORES]
        return stores

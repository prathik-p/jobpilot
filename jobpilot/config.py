"""Loads config.yaml, profile.yaml, companies.yaml and .env."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

try:
    from dotenv import load_dotenv
except ImportError:  # dotenv is optional
    load_dotenv = None

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    config: dict
    profile: dict
    companies: dict
    root: Path

    def path(self, key: str) -> Path:
        p = Path(self.config["paths"][key])
        return p if p.is_absolute() else self.root / p

    @property
    def dry_run(self) -> bool:
        return bool(self.config["apply"].get("dry_run", True))


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_settings(root: Path | None = None) -> Settings:
    root = Path(root or os.environ.get("JOBPILOT_ROOT", ROOT))
    if load_dotenv:
        load_dotenv(root / ".env")
    config = _load_yaml(root / "config.yaml")
    if not config:
        raise SystemExit(f"config.yaml not found in {root}. Copy config.example.yaml to config.yaml "
                         "and profile.example.yaml to profile.yaml, then edit them.")
    s = Settings(
        config=config,
        profile=_load_yaml(root / "profile.yaml"),
        companies=_load_yaml(root / "companies.yaml"),
        root=root,
    )
    s.path("output_dir").mkdir(parents=True, exist_ok=True)
    s.path("db").parent.mkdir(parents=True, exist_ok=True)
    return s


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()

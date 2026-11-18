from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Config:
    repo: Path
    research_model: str
    checker_model: str
    generator_model: str
    search_tool: str
    search_max_uses: int
    spend_cap_usd: float
    prices: dict  # model -> {"input": x, "output": y}
    web_search_usd: float
    cache_dir: Path
    out_dir: Path
    repo_fail: float = 0.55        # repo similarity at or above this fails a question
    max_attempts: int = 3          # rewrites per question
    chunk: int = 10                # questions per generation call
    web_check: bool = True
    backend: str = "auto"                       # auto | api | claude-code
    claude_code_models: dict = None             # optional model name overrides for the claude-code backend


def load(path: Path | None = None) -> Config:
    p = path or ROOT / "forge.toml"
    with open(p, "rb") as f:
        raw = tomllib.load(f)
    local = ROOT / "forge.local.toml"       # your own machine's settings (not committed); overrides forge.toml
    if path is None and local.is_file():
        with open(local, "rb") as f:
            for section, values in tomllib.load(f).items():
                if isinstance(values, dict):
                    raw.setdefault(section, {}).update(values)
                else:
                    raw[section] = values
    prices = {k: v for k, v in raw["prices"].items() if isinstance(v, dict)}
    return Config(
        repo=Path(raw["paths"]["repo"]),
        research_model=raw["models"]["research"],
        checker_model=raw["models"]["checker"],
        generator_model=raw["models"]["generator"],
        search_tool=raw["search"]["tool_type"],
        search_max_uses=int(raw["search"]["max_uses"]),
        spend_cap_usd=float(raw["limits"]["spend_cap_usd"]),
        prices=prices,
        web_search_usd=float(raw["prices"]["web_search_usd"]),
        cache_dir=ROOT / "cache",
        out_dir=ROOT / "out",
        repo_fail=float(raw.get("originality", {}).get("repo_fail", 0.55)),
        max_attempts=int(raw.get("generate", {}).get("max_attempts", 3)),
        chunk=int(raw.get("generate", {}).get("chunk", 10)),
        web_check=bool(raw.get("originality", {}).get("web_check", True)),
        backend=str(raw.get("backend", {}).get("kind", "auto")).lower(),
        claude_code_models=dict(raw.get("claude_code", {}).get("models", {})),
    )

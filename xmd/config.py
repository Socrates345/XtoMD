"""Settings (sources.yaml) + what-you-follow (sources/x.md) loading.

Only X is a live platform today, but Source/Config are shaped so a future
adapter (RSS/YouTube/Substack — see fetcher.py's docstring) slots in without
restructuring: Source.platform is already generic, and _read_md_list() only
needs a new call per list type.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger("xmd")

X_API_KEY_ENV_VAR = "XMD_TWITTERAPI_KEY"
TWEETAPI_API_KEY_ENV_VAR = "XMD_TWEETAPI_KEY"

# X backends: "twitterapi" (twitterapi.io, pay-per-tweet) and "tweetapi"
# (tweetapi.com, flat monthly request quota) — selected via x.backend in
# sources.yaml; fetcher.py picks the adapter module from config.x_backend.
X_BACKENDS = ("twitterapi", "tweetapi")


@dataclass
class Source:
    name: str
    type: str  # adapter dispatch key: "twitterapi" today
    handle: str = ""  # X handle, no "@"
    platform: str = "x"  # "x" today; a future adapter would add "rss" etc.
    group: str = ""  # interest group from a `## header` in sources/x.md ("" = none)
    priority: bool = False  # from a `| priority` field: sorts first within its section


@dataclass
class Config:
    sources: list[Source]
    max_per_source: int = 15  # cap items kept per handle per fetch (0 = unlimited)
    x_max_age_hours: int = 48  # stop paginating past this age (0 = unlimited)
    drop_retweets: bool = False  # quality filter: exclude X retweets
    drop_replies: bool = True  # quality filter: exclude X replies
    block_keywords: list[str] = field(default_factory=list)
    recap_groups: frozenset[str] = frozenset()  # groups marked `| recap` in sources/x.md (lowercased)
    snippet_chars: int = 140  # quick digest: length of a regular post's one-liner
    retweet_chars: int = 100  # quick digest: length of a plain retweet's one-liner
    media_chars: int = 500  # quick digest: length cap on image tweets, which are kept nearly whole
    trending_similarity: float = 0.4  # how alike two posts must be to count as the same story (0-1)
    x_backend: str = "twitterapi"  # "twitterapi" (twitterapi.io) | "tweetapi" (tweetapi.com)
    x_api_key: str = ""  # twitterapi.io API key
    tweetapi_api_key: str = ""  # tweetapi.com API key
    tweetapi_rate_limit_per_minute: int = 10  # tweetapi.com's request cap (free tier); raise to 60 on the paid plan
    storage: Path = field(default_factory=lambda: Path("xmd.db"))
    digest_dir: Path = field(default_factory=lambda: Path("digests"))

    @property
    def active_x_api_key(self) -> str:
        return self.tweetapi_api_key if self.x_backend == "tweetapi" else self.x_api_key


_LEGACY_AIM_PREFIXES = ("focus:", "aim:")


def _read_md_list(path: Path) -> list[dict]:
    """Read sources/x.md: one handle per line, optional `| Display Name`,
    `#` comments, blank lines ignored.

    - `## group` headers partition entries into interest groups, each
      becoming its own section in the rendered markdown. `## group | recap`
      marks a recap group: the quick digest collapses it to its global
      picture instead of listing every tweet.
    - a third `| priority` field marks a source as high-priority: its items
      sort first within their section, ahead of non-priority sources. A
      legacy `focus:`/`aim:` line (from the old RSS4.0 format, back when an
      LLM recap read them) is tolerated and skipped.

        karpathy

        ## finance
        DeItaone | Walter Bloomberg | priority
        unusual_whales

    Header detection runs before `#` comment stripping so `##` is never
    eaten as a comment.
    """
    entries: list[dict] = []
    group = ""
    recap = False
    # utf-8-sig: Windows editors add a BOM that would poison the first entry
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        stripped = line.strip()
        if stripped.startswith("##"):
            name, *options = (p.strip() for p in stripped.lstrip("#").split("|"))
            group = name.lower()
            recap = any(o.lower() == "recap" for o in options)
            continue
        line = line.split("#", 1)[0].strip()
        if not line or line.lower().startswith(_LEGACY_AIM_PREFIXES):
            continue
        parts = [p.strip() for p in line.split("|")]
        handle, name = parts[0].lstrip("@"), (parts[1] if len(parts) > 1 else "")
        priority = len(parts) > 2 and parts[2].strip().lower() == "priority"
        entries.append(
            {"handle": handle, "name": name, "group": group, "priority": priority, "recap": recap}
        )
    return entries


def load_config(path: str | Path = "sources.yaml") -> Config:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found — copy sources.example.yaml to {path} and edit it"
        )
    raw = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}

    x_cfg = raw.get("x") or {}
    x_backend = str(x_cfg.get("backend", "twitterapi"))
    if x_backend not in X_BACKENDS:
        raise ValueError(f"x.backend must be one of {X_BACKENDS}, got {x_backend!r}")
    x_api_key = x_cfg.get("api_key") or os.environ.get(X_API_KEY_ENV_VAR, "")
    tweetapi_api_key = x_cfg.get("tweetapi_api_key") or os.environ.get(TWEETAPI_API_KEY_ENV_VAR, "")
    tweetapi_rate_limit_per_minute = int(x_cfg.get("tweetapi_rate_limit_per_minute", 10))
    active_x_api_key = tweetapi_api_key if x_backend == "tweetapi" else x_api_key
    if not active_x_api_key:
        env_var = TWEETAPI_API_KEY_ENV_VAR if x_backend == "tweetapi" else X_API_KEY_ENV_VAR
        key_field = "tweetapi_api_key" if x_backend == "tweetapi" else "api_key"
        raise ValueError(f"x.{key_field} (or {env_var}) is not set")

    sources_dir = path.parent / raw.get("sources_dir", "sources")
    x_md = sources_dir / "x.md"
    if not x_md.exists():
        raise ValueError(f"no sources found — add handles to {x_md}")
    entries = _read_md_list(x_md)

    sources, seen = [], set()
    for entry in entries:
        handle = entry["handle"]
        if handle in seen:  # same handle listed twice -> fetch once
            continue
        seen.add(handle)
        sources.append(Source(
            name=entry["name"] or f"@{handle}", type="twitterapi",
            handle=handle, platform="x", group=entry["group"],
            priority=entry["priority"],
        ))
    if not sources:
        raise ValueError(f"no sources found — add handles to {x_md}")

    fetch = raw.get("fetch") or {}
    filters = raw.get("filters") or {}
    digest = raw.get("digest") or {}
    trending_similarity = float(digest.get("trending_similarity", 0.4))
    if not 0 < trending_similarity <= 1:
        raise ValueError(f"digest.trending_similarity must be in (0, 1], got {trending_similarity}")

    return Config(
        sources=sources,
        recap_groups=frozenset(e["group"] for e in entries if e["recap"] and e["group"]),
        snippet_chars=int(digest.get("snippet_chars", 140)),
        retweet_chars=int(digest.get("retweet_chars", 100)),
        media_chars=int(digest.get("media_chars", 500)),
        trending_similarity=trending_similarity,
        max_per_source=int(fetch.get("max_per_source", 15)),
        x_max_age_hours=int(fetch.get("max_age_hours", 48)),
        drop_retweets=bool(filters.get("drop_retweets", False)),
        drop_replies=bool(filters.get("drop_replies", True)),
        block_keywords=[str(k) for k in (filters.get("block_keywords") or [])],
        x_backend=x_backend,
        x_api_key=x_api_key,
        tweetapi_api_key=tweetapi_api_key,
        tweetapi_rate_limit_per_minute=tweetapi_rate_limit_per_minute,
        storage=Path(raw.get("storage", "xmd.db")),
        digest_dir=Path(raw.get("digest_dir", "digests")),
    )

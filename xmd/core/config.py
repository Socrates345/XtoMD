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
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

import yaml

log = logging.getLogger("xmd")

X_API_KEY_ENV_VAR = "XMD_TWITTERAPI_KEY"
TWEETAPI_API_KEY_ENV_VAR = "XMD_TWEETAPI_KEY"

# X backends: "twitterapi" (twitterapi.io, pay-per-tweet) and "tweetapi"
# (tweetapi.com, flat monthly request quota) — selected via x.backend in
# sources.yaml; fetcher.py picks the adapter module from config.x_backend.
X_BACKENDS = ("twitterapi", "tweetapi")

# the clock the brief's file name and header are read in (stored times and digest stamps stay UTC)
DEFAULT_TIMEZONE = "Europe/Paris"
# where the digests, the exports, the runs and the briefs go when `digest_dir:` names no folder
DEFAULT_DIGEST_DIR = "digests"

# languages `language:` in sources.yaml may name, by ISO 639-1 code or English name. The code is what the model is
# asked to tag a post with, the name is what it is told to write in.
LANGUAGE_NAMES = {
    "ar": "Arabic", "bg": "Bulgarian", "bn": "Bengali", "ca": "Catalan", "cs": "Czech", "da": "Danish",
    "de": "German", "el": "Greek", "en": "English", "es": "Spanish", "et": "Estonian", "fa": "Persian",
    "fi": "Finnish", "fr": "French", "he": "Hebrew", "hi": "Hindi", "hr": "Croatian", "hu": "Hungarian",
    "id": "Indonesian", "it": "Italian", "ja": "Japanese", "ko": "Korean", "lt": "Lithuanian", "lv": "Latvian",
    "ms": "Malay", "nl": "Dutch", "no": "Norwegian", "pl": "Polish", "pt": "Portuguese", "ro": "Romanian",
    "ru": "Russian", "sk": "Slovak", "sl": "Slovenian", "sr": "Serbian", "sv": "Swedish", "th": "Thai",
    "tr": "Turkish", "uk": "Ukrainian", "ur": "Urdu", "vi": "Vietnamese", "zh": "Chinese",
}
_LANGUAGE_CODES = {name.lower(): code for code, name in LANGUAGE_NAMES.items()}


@dataclass(frozen=True)
class Language:
    """`language:` in sources.yaml: the languages the reader reads, and the one of them everything else is
    translated into. Only the model's side of the brief uses it (run_system.py, assemble_brief.py): the store and
    the digests keep every post as it was written."""

    accepted: tuple[str, ...]  # ISO 639-1 codes; posts in these are left in their own language
    translate_to: str  # one of `accepted`

    def name(self, code: str) -> str:
        return LANGUAGE_NAMES.get(code, code)

    @property
    def accepted_names(self) -> str:
        return ", ".join(self.name(c) for c in self.accepted)


@dataclass
class Source:
    name: str
    type: str  # adapter dispatch key: "twitterapi" today
    handle: str = ""  # X handle, no "@"
    platform: str = "x"  # "x" today; a future adapter would add "rss" etc.
    group: str = ""  # interest group from a `## header` in sources/x.md ("" = none)
    priority: bool = False  # `priority:high` in sources/x.md: sorts first within its section
    reposts: bool = True  # `reposts:off`, its own or its group's: its plain retweets are dropped at fetch
    only: tuple[str, ...] = ()  # `only: a, b`: of its tweets, just those with one of these keywords are kept
    hide: tuple[str, ...] = ()  # `hide: a, b`: its tweets with one of these keywords are dropped


@dataclass
class Config:
    sources: list[Source]
    max_per_source: int = 15  # tweets fetched per handle per fetch: up to 2x this; 0 = no limit on tweetapi, a ceiling of 100 elsewhere (fetcher._fetch_ceiling)
    x_max_age_hours: int = 48  # stop paginating past this age (0 = unlimited)
    drop_retweets: bool = False  # quality filter: exclude X retweets
    drop_replies: bool = True  # quality filter: exclude X replies
    block_keywords: list[str] = field(default_factory=list)
    recap_groups: frozenset[str] = frozenset()  # groups marked `recap:on` in sources/x.md (lowercased)
    group_levels: dict[str, str] = field(default_factory=dict)  # group (lowercased) -> "high" | "low"; absent = normal
    snippet_chars: int = 140  # quick digest: length of a regular post's one-liner
    retweet_chars: int = 100  # quick digest: length of a plain retweet's one-liner
    media_chars: int = 500  # quick digest: length cap on image tweets, which are kept nearly whole
    trending_similarity: float = 0.4  # how alike two posts must be to count as the same story (0-1)
    x_backend: str = "twitterapi"  # "twitterapi" (twitterapi.io) | "tweetapi" (tweetapi.com)
    x_api_key: str = ""  # twitterapi.io API key
    tweetapi_api_key: str = ""  # tweetapi.com API key
    tweetapi_rate_limit_per_minute: int = 10  # tweetapi.com's request cap (free tier); raise to 60 on the paid plan
    storage: Path = field(default_factory=lambda: Path("xmd.db"))
    digest_dir: Path = field(default_factory=lambda: Path(DEFAULT_DIGEST_DIR))
    tz: ZoneInfo = field(default_factory=lambda: ZoneInfo(DEFAULT_TIMEZONE))  # `timezone:` in sources.yaml
    language: Language | None = None  # `language:` in sources.yaml; None = every post stays in its own language

    @property
    def active_x_api_key(self) -> str:
        return self.tweetapi_api_key if self.x_backend == "tweetapi" else self.x_api_key


_LEGACY_AIM_PREFIXES = ("focus:", "aim:")

# The variables of sources/x.md, written `variable:value` in the `|` fields after a handle or a group's name. Their
# names and the words their values take are spelled here and nowhere else in the code: to rename one, change it
# here, then where the docs and the tests quote it (docs/usage.md, sources.example/x.md, tests/core/test_config.py).
NAME = "name"  # a handle: the name shown instead of @handle
PRIORITY = "priority"  # a handle: `high` = its tweets sort first and are shown in full
REPOSTS = "reposts"  # a handle or a group: `off` = its plain retweets are dropped at fetch
ONLY = "only"  # a handle or a group: keep only the tweets with one of these keywords
HIDE = "hide"  # a handle or a group: drop the tweets with one of these keywords
LEVEL = "level"  # a group: how much of it the brief keeps
RECAP = "recap"  # a group: `on` = only worth its overall picture
# what a group header may say about its importance; "normal" (also written medium or regular) is the default
LEVEL_WORDS = {"high": "high", "low": "low", "normal": "normal", "medium": "normal", "regular": "normal"}
# the words each variable's value takes -> what the rest of the code is given
_VALUES: dict[str, dict] = {
    PRIORITY: {"high": True, "normal": False},
    REPOSTS: {"on": True, "off": False},
    RECAP: {"on": True, "off": False},
    LEVEL: LEVEL_WORDS,
}
_HANDLE_VARIABLES = (NAME, PRIORITY, REPOSTS, ONLY, HIDE)
_GROUP_VARIABLES = (LEVEL, RECAP, REPOSTS, ONLY, HIDE)
# how a list written before `variable:value` said the same things, with a bare word in a field: still read. These
# are the old words, not the ones above: renaming a variable or a value leaves them alone
_BARE_WORDS: dict[str, tuple] = {
    "priority": (PRIORITY, True), "rp_off": (REPOSTS, False), "recap": (RECAP, True),
    "high": (LEVEL, "high"), "low": (LEVEL, "low"),
    "normal": (LEVEL, "normal"), "medium": (LEVEL, "normal"), "regular": (LEVEL, "normal"),
}
_QUOTES = '"“”'  # the curly ones are what a phone keyboard types
# what starts a comment. `//` is the one to write: a line that opens with `#` is a heading to whatever renders the
# markdown (Obsidian, GitHub). `#` is still read, so a list written before `//` existed loads as it did
_COMMENT_MARKS = ("//", "#")


def _split(text: str, sep: str) -> list[str]:
    """`text` cut at each `sep` that is not inside double quotes, which stay in: a quoted keyword may hold a
    `#`, a `//`, a `,` or a `|` (`hide: "#ad"`)."""
    parts, quoted, k = [""], False, 0
    while k < len(text):
        if text[k] in _QUOTES:
            quoted = not quoted
        if not quoted and text.startswith(sep, k):
            parts.append("")
            k += len(sep)
        else:
            parts[-1] += text[k]
            k += 1
    return parts


def _uncomment(text: str) -> str:
    for mark in _COMMENT_MARKS:
        text = _split(text, mark)[0]
    return text


def _read_fields(fields: list[str], variables: tuple[str, ...], where: str) -> dict:
    """{variable: value} of the `|` fields after a handle or a group's name, for the `variables` that line may
    set; one it does not set is not in the result. A variable written twice keeps its first value, keywords add
    up. `where` names the line in a warning."""
    found: dict = {}
    other: list[str] = []  # the fields that are no `variable:value`
    for field in (f.strip() for f in fields):
        key, colon, value = (p.strip() for p in field.partition(":"))
        key = key.lower()
        if not colon or key not in variables:
            variable, meant = _BARE_WORDS.get(field.lower(), ("", None))
            if variable in variables:
                found.setdefault(variable, meant)
            elif field:
                other.append(field)
        elif key in (ONLY, HIDE):
            words = tuple(w for w in (p.strip().strip(_QUOTES).strip() for p in _split(value, ",")) if w)
            if not words:  # `only:` with nothing after it must not mean "keep nothing"
                log.warning("sources/x.md: `%s:` on %s names no keyword and is ignored (a keyword holding a #, a // or"
                            " a comma goes in double quotes)", key, where)
            found[key] = found.get(key, ()) + words
        elif key == NAME:
            if len(value) > 1 and value[0] in _QUOTES and value[-1] in _QUOTES:  # quoted, a name may hold a # or a |
                value = value[1:-1].strip()
            found.setdefault(key, value)
        elif value.lower() in _VALUES[key]:
            found.setdefault(key, _VALUES[key][value.lower()])
        else:  # `priority:hgih` must not silently mean normal
            log.warning("sources/x.md: unknown value %r for `%s:` on %s is ignored (use %s)",
                        value, key, where, ", ".join(_VALUES[key]))
    if NAME in variables and NAME not in found and other:
        found[NAME] = other.pop(0)  # a list written before `name:`: the first field that is no variable is the name
    for field in other:  # a typo like "hgih" must not silently do nothing
        log.warning("sources/x.md: unknown option %r on %s is ignored (use %s)",
                    field, where, ", ".join(f"{v}:" for v in variables))
    return found


def source_options(source: Source) -> list[str]:
    """What a source sets beyond the defaults, the way sources/x.md writes it (`priority:high`): for `xmd sources`."""
    def word(variable: str, meant: bool) -> str:
        return next(w for w, value in _VALUES[variable].items() if value is meant)

    said = [f"{PRIORITY}:{word(PRIORITY, True)}"] if source.priority else []
    if not source.reposts:
        said.append(f"{REPOSTS}:{word(REPOSTS, False)}")
    return said + [f"{variable}:{', '.join(words)}" for variable, words in ((ONLY, source.only), (HIDE, source.hide)) if words]


def _read_md_list(path: Path) -> list[dict]:
    """Read sources/x.md: one handle per line, `//` comments (`#` ones too, see _COMMENT_MARKS), blank lines
    ignored. What a line says beyond its handle is written `variable:value` in `|` fields, in any order: the
    variables and the values they take are the table above.

    - `## group` headers partition entries into interest groups, each
      becoming its own section in the rendered markdown. `recap:on` on one
      marks a recap group: the quick digest collapses it to its global
      picture instead of listing every tweet.
    - `level:high` or `level:low` on a header says how much of the group the LLM brief keeps: a
      high group gets more detail, a low one is condensed harder; no level means normal.
      A recap group is always the lowest, so a level written on it (`## chat | recap:on | level:low`)
      is accepted and means nothing.
    - `priority:high` on a handle marks a high-priority source: its items sort first within
      their section, ahead of non-priority sources. `reposts:off`, `only: a, b` and
      `hide: a, b` are a source's filters (see Source, and ingest/filters.py). On a header they
      are the group's: `reposts` holds for each of its handles that does not say otherwise, the
      keywords are added to each handle's own.
    - a list written before `variable:value` still loads: a bare `priority`, `rp_off`, `recap`,
      `high` or `low` in a field (see _BARE_WORDS), and the first field that is no variable at
      all is the display name. A legacy `focus:`/`aim:` line (from the old RSS4.0 format, back
      when an LLM recap read them) is tolerated and skipped.

        alice

        ## finance | level:high | hide:presale
        NewsFrank | name:Frank Wire | priority:high
        gina_alerts | reposts:off | only:airdrop, "#alpha"

    Header detection runs before `#` comment stripping so `##` is never
    eaten as a comment.
    """
    entries: list[dict] = []
    group = ""
    of_group: dict = {}  # what the header of the current group set
    # utf-8-sig: Windows editors add a BOM that would poison the first entry
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        stripped = line.strip()
        if stripped.startswith("##"):
            # a `#` comment is cut from the options only: the group's name is left as written, `#` included
            name, _, options = _split(stripped.lstrip("#"), "//")[0].partition("|")
            group = name.strip().lower()
            of_group = _read_fields(_split(_split(options, "#")[0], "|"), _GROUP_VARIABLES, f"group {group!r}")
            continue
        line = _uncomment(line).strip()
        if not line or line.lower().startswith(_LEGACY_AIM_PREFIXES):
            continue
        parts = _split(line, "|")
        handle = parts[0].strip().lstrip("@")
        said = _read_fields(parts[1:], _HANDLE_VARIABLES, f"@{handle}")
        entries.append({
            "handle": handle, "name": said.get(NAME, ""), "group": group, "priority": said.get(PRIORITY, False),
            "reposts": said.get(REPOSTS, of_group.get(REPOSTS, True)),
            "only": of_group.get(ONLY, ()) + said.get(ONLY, ()), "hide": of_group.get(HIDE, ()) + said.get(HIDE, ()),
            "recap": of_group.get(RECAP, False), "level": of_group.get(LEVEL, "normal"),
        })
    return entries


def _levels(entries: list[dict]) -> dict[str, str]:
    return {e["group"]: e["level"] for e in entries if e["group"] and e["level"] != "normal" and not e["recap"]}


def read_group_levels(x_md: Path) -> dict[str, str]:
    """{group: "high" | "low"} from a sources/x.md, for the groups that state a level. Nothing else is
    taken out of the file, so the brief can read its groups without loading the whole config."""
    return _levels(_read_md_list(Path(x_md)))


def _section(raw: dict, name: str, path: Path) -> dict:
    """A top-level group of settings (`x:`, `fetch:`, ...); empty or missing is fine, anything else than
    `key: value` lines is a mistake to name rather than an AttributeError to trace."""
    value = raw.get(name) or {}
    if not isinstance(value, dict):
        raise ValueError(f"{path}: `{name}:` must be a group of settings, not a {type(value).__name__}")
    return value


def _path_setting(raw: dict, name: str, default: str, path: Path) -> Path:
    """A top-level path (`storage:`, `digest_dir:`, `sources_dir:`). Missing, or left with nothing after the colon,
    means the default; anything but text is a mistake to name rather than a TypeError to trace."""
    value = raw.get(name)
    if value is None or value == "":
        return Path(default)
    if not isinstance(value, str):
        raise ValueError(f"{path}: `{name}:` must be a path, not a {type(value).__name__}")
    return Path(value)


def _language_code(value: object, path: Path) -> str:
    text = str(value).strip().lower()
    if text in LANGUAGE_NAMES:
        return text
    if text in _LANGUAGE_CODES:
        return _LANGUAGE_CODES[text]
    raise ValueError(f"{path}: unknown language {value!r} in `language:` — use an ISO 639-1 code (en, fr, de, zh, ...)"
                     " or its English name")


def _language(raw: dict, path: Path) -> Language | None:
    """`language:` read into a Language, or None when it is missing or empty. `translate_to` alone means that is
    the only language read; `accepted` alone translates into the first of them."""
    lang = _section(raw, "language", path)
    accepted = lang.get("accepted") or []
    if isinstance(accepted, str):
        accepted = [accepted]
    if not isinstance(accepted, list):
        raise ValueError(f"{path}: language.accepted must be a list, e.g. [en, fr]")
    codes = list(dict.fromkeys(_language_code(a, path) for a in accepted))
    target = _language_code(lang["translate_to"], path) if lang.get("translate_to") else ""
    if not codes and not target:
        return None
    if not codes:
        codes = [target]
    if not target:
        target = codes[0]
    if target not in codes:
        raise ValueError(f"{path}: language.translate_to ({target}) must be one of language.accepted ({', '.join(codes)})")
    return Language(tuple(codes), target)


def _read_yaml(path: Path) -> dict:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    except yaml.YAMLError as exc:
        # PyYAML's own message quotes the lines around the mistake, and the api key is often one of them: say
        # where the mistake is, never what the file says (`from None` keeps the chained cause out of a traceback too)
        mark = getattr(exc, "problem_mark", None)
        raise ValueError(f"{path} is not valid YAML" + (f" (line {mark.line + 1})" if mark else "")) from None
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must be a mapping of settings (x:, fetch:, ...), not a {type(raw).__name__}")
    return raw


def read_language(path: str | Path = "sources.yaml") -> Language | None:
    """`language:` from a sources.yaml, and nothing else, so the model step can read it without the api key or the
    follow list load_config insists on. A missing file means no language settings."""
    path = Path(path)
    return _language(_read_yaml(path), path) if path.exists() else None


def read_digest_dir(path: str | Path = "sources.yaml") -> Path:
    """`digest_dir:` from a sources.yaml, and nothing else: deploy/common.sh needs the folder where .env has not been
    read, so without the api key or the follow list load_config insists on. A missing file means the default."""
    path = Path(path)
    return _path_setting(_read_yaml(path) if path.exists() else {}, "digest_dir", DEFAULT_DIGEST_DIR, path)


def load_config(path: str | Path = "sources.yaml") -> Config:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found — copy sources.example.yaml to {path} and edit it"
        )
    raw = _read_yaml(path)

    x_cfg = _section(raw, "x", path)
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

    sources_dir = path.parent / _path_setting(raw, "sources_dir", "sources", path)
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
            priority=entry["priority"], reposts=entry["reposts"], only=entry["only"], hide=entry["hide"],
        ))
    if not sources:
        raise ValueError(f"no sources found — add handles to {x_md}")

    fetch = _section(raw, "fetch", path)
    filters = _section(raw, "filters", path)
    digest = _section(raw, "digest", path)
    trending_similarity = float(digest.get("trending_similarity", 0.4))
    if not 0 < trending_similarity <= 1:
        raise ValueError(f"digest.trending_similarity must be in (0, 1], got {trending_similarity}")

    tz_name = str(raw.get("timezone") or DEFAULT_TIMEZONE)
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        if not available_timezones():  # no tz database at all (Windows without the tzdata package)
            raise ValueError(
                f"can't resolve timezone {tz_name!r}: no timezone database found. Run `pip install tzdata`"
                " (or reinstall with `pip install -e .`)"
            ) from None
        raise ValueError(f"timezone must be an IANA name like Europe/Paris or UTC, got {tz_name!r}") from None

    return Config(
        sources=sources,
        recap_groups=frozenset(e["group"] for e in entries if e["recap"] and e["group"]),
        group_levels=_levels(entries),
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
        storage=_path_setting(raw, "storage", "xmd.db", path),
        digest_dir=_path_setting(raw, "digest_dir", DEFAULT_DIGEST_DIR, path),
        tz=tz,
        language=_language(raw, path),
    )

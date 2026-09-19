import pytest

from xmd.config import load_config

X_KEY_YAML = "x:\n  api_key: test-key\n"


def _setup(tmp_path, yaml_text=X_KEY_YAML, x_md="karpathy\n"):
    (tmp_path / "sources").mkdir(exist_ok=True)
    (tmp_path / "sources" / "x.md").write_text(x_md, encoding="utf-8")
    path = tmp_path / "sources.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    return path


def test_basic_handle_parsing(tmp_path):
    path = _setup(tmp_path, x_md="@karpathy\n# a comment\n\npaulg | Paul Graham\n")
    cfg = load_config(path)
    by_name = {s.name: s for s in cfg.sources}

    assert len(cfg.sources) == 2
    assert (by_name["@karpathy"].type, by_name["@karpathy"].handle) == ("twitterapi", "karpathy")
    assert by_name["Paul Graham"].handle == "paulg"


X_MD_WITH_GROUPS = """\
karpathy | Andrej   # above any header -> default group

## Finance
focus: oil trades, energy markets
DeItaone | Walter Bloomberg
unusual_whales

## science
aim: research talks
paulg
"""


def test_groups_and_legacy_focus_lines_are_skipped(tmp_path):
    path = _setup(tmp_path, x_md=X_MD_WITH_GROUPS)
    cfg = load_config(path)

    by_handle = {s.handle: s for s in cfg.sources}
    assert by_handle["karpathy"].group == ""  # pre-header -> default
    assert by_handle["karpathy"].name == "Andrej"  # inline comment stripped
    assert by_handle["DeItaone"].group == "finance"  # header lowercased
    assert by_handle["unusual_whales"].group == "finance"
    assert by_handle["paulg"].group == "science"
    # legacy focus:/aim: lines (from the old LLM-recap era) must not be
    # misparsed as bogus handles
    assert {s.handle for s in cfg.sources} == {"karpathy", "DeItaone", "unusual_whales", "paulg"}


def test_priority_field_marks_source_priority(tmp_path):
    path = _setup(
        tmp_path,
        x_md="DeItaone | Walter Bloomberg | priority\nunusual_whales | | priority\nkarpathy\n",
    )
    cfg = load_config(path)
    by_handle = {s.handle: s for s in cfg.sources}
    assert by_handle["DeItaone"].priority is True
    assert by_handle["unusual_whales"].priority is True
    assert by_handle["karpathy"].priority is False


def test_x_backend_defaults_to_twitterapi(tmp_path):
    path = _setup(tmp_path)
    assert load_config(path).x_backend == "twitterapi"


def test_x_backend_tweetapi_uses_its_own_key(tmp_path):
    path = _setup(tmp_path, yaml_text="x:\n  backend: tweetapi\n  tweetapi_api_key: tw-secret\n")
    cfg = load_config(path)
    assert cfg.x_backend == "tweetapi"
    assert cfg.tweetapi_api_key == "tw-secret"
    assert cfg.active_x_api_key == "tw-secret"


def test_x_backend_tweetapi_requires_its_own_key_not_twitterapis(tmp_path):
    path = _setup(tmp_path, yaml_text="x:\n  backend: tweetapi\n  api_key: irrelevant\n")
    with pytest.raises(ValueError, match="tweetapi_api_key"):
        load_config(path)


def test_invalid_x_backend_rejected(tmp_path):
    path = _setup(tmp_path, yaml_text="x:\n  backend: bogus\n  api_key: k\n")
    with pytest.raises(ValueError, match="x.backend"):
        load_config(path)


def test_api_key_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("XMD_TWITTERAPI_KEY", "env-secret")
    path = _setup(tmp_path, yaml_text="")
    assert load_config(path).x_api_key == "env-secret"


def test_tweetapi_key_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("XMD_TWEETAPI_KEY", "env-secret")
    path = _setup(tmp_path, yaml_text="x:\n  backend: tweetapi\n")
    assert load_config(path).tweetapi_api_key == "env-secret"


def test_tweetapi_rate_limit_defaults_to_10(tmp_path):
    path = _setup(tmp_path, yaml_text="x:\n  backend: tweetapi\n  tweetapi_api_key: tw-secret\n")
    assert load_config(path).tweetapi_rate_limit_per_minute == 10


def test_tweetapi_rate_limit_from_yaml(tmp_path):
    path = _setup(
        tmp_path,
        yaml_text="x:\n  backend: tweetapi\n  tweetapi_api_key: tw-secret\n"
        "  tweetapi_rate_limit_per_minute: 60\n",
    )
    assert load_config(path).tweetapi_rate_limit_per_minute == 60


def test_missing_api_key_raises(tmp_path):
    path = _setup(tmp_path, yaml_text="")
    with pytest.raises(ValueError, match="api_key"):
        load_config(path)


def test_missing_config_file():
    with pytest.raises(FileNotFoundError):
        load_config("does-not-exist.yaml")


def test_no_sources_gives_actionable_error(tmp_path):
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "x.md").write_text("", encoding="utf-8")
    path = tmp_path / "sources.yaml"
    path.write_text(X_KEY_YAML, encoding="utf-8")
    with pytest.raises(ValueError, match=r"x\.md"):
        load_config(path)


def test_missing_x_md_gives_actionable_error(tmp_path):
    (tmp_path / "sources").mkdir()
    path = tmp_path / "sources.yaml"
    path.write_text(X_KEY_YAML, encoding="utf-8")
    with pytest.raises(ValueError, match=r"x\.md"):
        load_config(path)


def test_duplicate_handles_collapsed(tmp_path):
    path = _setup(tmp_path, x_md="karpathy\nkarpathy\n")
    assert len(load_config(path).sources) == 1


def test_custom_sources_dir(tmp_path):
    (tmp_path / "mylists").mkdir()
    (tmp_path / "mylists" / "x.md").write_text("karpathy\n", encoding="utf-8")
    path = tmp_path / "sources.yaml"
    path.write_text("sources_dir: mylists\nx:\n  api_key: test-key\n", encoding="utf-8")
    assert len(load_config(path).sources) == 1


def test_fetch_defaults(tmp_path):
    path = _setup(tmp_path)
    cfg = load_config(path)
    assert cfg.max_per_source == 15
    assert cfg.x_max_age_hours == 48
    assert str(cfg.storage) == "xmd.db"
    assert str(cfg.digest_dir) == "digests"


def test_fetch_settings_from_yaml(tmp_path):
    path = _setup(tmp_path, yaml_text=X_KEY_YAML + "fetch:\n  max_per_source: 5\n  max_age_hours: 12\n")
    cfg = load_config(path)
    assert cfg.max_per_source == 5
    assert cfg.x_max_age_hours == 12


def test_filter_defaults(tmp_path):
    path = _setup(tmp_path)
    cfg = load_config(path)
    assert cfg.drop_retweets is False  # retweets included, per this tool's brief
    assert cfg.drop_replies is True
    assert cfg.block_keywords == []


def test_filter_settings_from_yaml(tmp_path):
    path = _setup(tmp_path, yaml_text=X_KEY_YAML + 'filters:\n  drop_retweets: true\n  block_keywords: ["ad"]\n')
    cfg = load_config(path)
    assert cfg.drop_retweets is True
    assert cfg.block_keywords == ["ad"]


def test_recap_option_on_a_group_header_marks_that_group_only(tmp_path):
    path = _setup(
        tmp_path,
        x_md="karpathy\n## Chatter | recap\npaulg\n## finance\nDeItaone | Walter | priority\n",
    )
    cfg = load_config(path)
    assert cfg.recap_groups == frozenset({"chatter"})  # lowercased like the group itself
    by_handle = {s.handle: s for s in cfg.sources}
    assert by_handle["paulg"].group == "chatter"  # the option doesn't leak into the name
    assert by_handle["DeItaone"].group == "finance"  # the next header resets it


def test_recap_option_is_case_and_space_tolerant(tmp_path):
    path = _setup(tmp_path, x_md="##  spammer but interesting  |  RECAP \npaulg\n")
    assert load_config(path).recap_groups == frozenset({"spammer but interesting"})


def test_no_recap_groups_by_default(tmp_path):
    assert load_config(_setup(tmp_path, x_md=X_MD_WITH_GROUPS)).recap_groups == frozenset()


def test_digest_defaults(tmp_path):
    cfg = load_config(_setup(tmp_path))
    assert (cfg.snippet_chars, cfg.retweet_chars, cfg.media_chars) == (140, 100, 500)
    assert cfg.trending_similarity == 0.4


def test_digest_settings_from_yaml(tmp_path):
    yaml_text = X_KEY_YAML + "digest:\n  snippet_chars: 90\n  retweet_chars: 60\n  media_chars: 300\n  trending_similarity: 0.55\n"
    cfg = load_config(_setup(tmp_path, yaml_text=yaml_text))
    assert (cfg.snippet_chars, cfg.retweet_chars, cfg.media_chars) == (90, 60, 300)
    assert cfg.trending_similarity == 0.55


@pytest.mark.parametrize("value", ["0", "1.5", "-0.2"])
def test_trending_similarity_must_be_a_fraction(tmp_path, value):
    path = _setup(tmp_path, yaml_text=X_KEY_YAML + f"digest:\n  trending_similarity: {value}\n")
    with pytest.raises(ValueError, match="trending_similarity"):
        load_config(path)

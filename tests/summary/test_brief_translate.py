from datetime import datetime, timezone

from xmd.core.models import FeedItem
from xmd.digest.export import build_export
from xmd.summary.brief import build_brief
from xmd.summary.chunk import parse_export
from xmd.summary.translate import Translation

NOW = datetime(2026, 9, 19, 16, 0, tzinfo=timezone.utc)
PRIORITY = frozenset({"@vip"})
ZH = "央行今天宣布降息"
ZH_EN = "The central bank announced a rate cut today"
DE = "Die Zentralbank senkt die Zinsen deutlich"
DE_EN = "The central bank cuts rates sharply"


def _post(n, source, text, group="", images=None, **kw) -> FeedItem:
    return FeedItem(
        source=source, source_type="x", title=text[:60], url=f"https://x.com/{source.lstrip('@')}/status/{n}",
        published=datetime(2026, 9, 19, 6, n, tzinfo=timezone.utc), full_text=text, group=group,
        images=images or [], **kw,
    )


ITEMS = [
    _post(1, "@vip", ZH, group="finance"),
    _post(2, "@pic", DE, group="business", images=["https://img.example/2.jpg"]),
    _post(3, "@b1", "an English post", group="business"),
    _post(4, "@r1", DE, group="business", retweet_of_author="bank", retweet_of_text=DE),  # a plain retweet
]
EXPORT_TEXT, MAPPING = build_export(ITEMS, NOW, priority_sources=PRIORITY)
POSTS, _ = parse_export(EXPORT_TEXT, MAPPING)
TRANSLATIONS = {ZH: Translation("zh", ZH_EN), DE: Translation("de", DE_EN, ("12",))}


def _brief(translated):
    return build_brief(ITEMS, NOW, [], MAPPING, {p.n: p.text for p in POSTS}, [], priority_sources=PRIORITY,
                       translated=translated).markdown


def test_without_translations_the_brief_is_as_before():
    assert _brief(None) == _brief(lambda text: None)


def test_every_text_shown_word_for_word_is_asked_about():
    asked: list[str] = []
    _brief(asked.append)
    assert {ZH, DE, "an English post"} <= set(asked)


def test_a_translated_priority_tweet_shows_the_translation_then_the_original_quoted():
    md = _brief(TRANSLATIONS.get)
    priority = md.split("## ★ Priority", 1)[1]
    assert priority.index(ZH_EN) < priority.index(f"> 🌐 *Chinese:* {ZH}")


def test_a_translated_image_caption_and_one_liner_keep_the_original_and_the_warning():
    md = _brief(TRANSLATIONS.get)
    images = md.split("### 🖼 Images", 1)[1]
    assert f"{DE_EN} [↗](https://x.com/pic/status/2) ⚠ not in the original: 12" in images
    assert f"\n> 🌐 *German:* {DE}" in images
    # the failed (here: never run) chunk's posts are one-liners; the plain retweet shows its original's translation
    loose = md.split("### Not summarized", 1)[1]
    assert f"@bank · {DE_EN}" in loose and f"\n  > 🌐 *German:* {DE}" in loose
    assert "an English post" in loose and "🌐 *English" not in md  # an untranslated post has no quote


def test_a_translated_trending_story_keeps_its_original_quoted_under_it():
    story = "Die Zentralbank senkt heute überraschend die Leitzinsen um einen halben Punkt"
    items = ITEMS + [_post(5, "@d1", story, group="news"), _post(6, "@d2", story, group="news")]
    export, mapping = build_export(items, NOW, priority_sources=PRIORITY)
    posts, _ = parse_export(export, mapping)
    md = build_brief(items, NOW, [], mapping, {p.n: p.text for p in posts}, [], priority_sources=PRIORITY,
                     translated={story: Translation("de", "The central bank surprises with a half-point cut")}.get
                     ).markdown
    trending = md.split("## Trending", 1)[1].split("## X /", 1)[0]
    assert "— The central bank surprises with a half-point cut\n  > 🌐 *German:* Die Zentralbank" in trending

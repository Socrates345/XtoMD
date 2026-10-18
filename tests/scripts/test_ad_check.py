"""The ad check from end to end: a digest of three posts, run_system.py with a fake model, assemble_brief.py."""

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))  # the scripts are run from a checkout, not installed

import assemble_brief  # noqa: E402
import run_system  # noqa: E402
from xmd import cli  # noqa: E402
from xmd.core.config import load_config  # noqa: E402
from xmd.core.models import FeedItem  # noqa: E402
from xmd.core.store import Store  # noqa: E402
from xmd.summary.ads import ADS_SCHEMA  # noqa: E402
from xmd.summary.engine import Completion  # noqa: E402

PITCH = "We made it so you can have your own console. See more: https://unusualwhales.com/console"
NEWS = "BREAKING: The Fed holds rates steady"
OTHER = "Globex shares jump after earnings"


class FakeEngine:
    """Calls a post with "See more" an ad, and writes one summary item per post it is given."""

    users: list[str] = []
    ad_checks: list[str] = []  # what the ad check was asked

    def __init__(self, *args, **kwargs):
        FakeEngine.users, FakeEngine.ad_checks = [], []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def complete(self, system, user, schema=None, max_tokens=16, temperature=0.2):
        return Completion("ok", "stop", 1, 1, 0, 0.0)

    def complete_json(self, system, user, schema, max_tokens=1500, temperature=0.2):
        FakeEngine.users.append(user)
        numbers = [int(n) for n in re.findall(r"^\[(\d+)\]", user, re.MULTILINE)]
        if schema is ADS_SCHEMA:
            FakeEngine.ad_checks.append(user)
            reply = {"posts": [{"n": n, "ad": "See more" in line}
                               for n, line in zip(numbers, (l for l in user.splitlines() if l.startswith("[")))]}
        else:
            reply = {"items": [{"headline": f"post {n}", "detail": "what it says", "ids": [n]} for n in numbers]}
        return reply, Completion(json.dumps(reply), "stop", 100, 30, 0, 0.1)


def _world(tmp_path, x_md):
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "x.md").write_text(x_md, encoding="utf-8")
    config_path = tmp_path / "sources.yaml"
    config_path.write_text(f"x:\n  api_key: test-key\nstorage: {(tmp_path / 'xmd.db').as_posix()}\n"
                           f"digest_dir: {(tmp_path / 'digests').as_posix()}\n", encoding="utf-8")
    hour = datetime.now(timezone.utc) - timedelta(hours=1)

    def post(author, n, text, images=()):
        return FeedItem(source=f"@{author}", source_type="x", title=text, author=author, full_text=text,
                        url=f"https://x.com/{author}/status/{n}", published=hour, images=list(images))

    pitch = post("unusual_whales", 1, PITCH, ["https://pbs.twimg.com/amplify_video_thumb/1/img/a.jpg"])
    store = Store(tmp_path / "xmd.db")
    store.add_items([pitch, post("unusual_whales", 2, NEWS), post("dave", 3, OTHER)])
    store.close()
    export = cli.run_digest(load_config(config_path), "24h")
    return config_path, export, pitch


def _run(tmp_path, monkeypatch, x_md):
    config_path, export, pitch = _world(tmp_path, x_md)
    monkeypatch.setattr(run_system, "Engine", FakeEngine)
    monkeypatch.setattr(run_system, "list_models", lambda *args: ["fake"])
    digests = tmp_path / "digests"
    run_system.main(["--export", str(export), "--digests-dir", str(digests), "--sources-dir", str(tmp_path / "sources"),
                     "--config", str(config_path), "--model", "fake", "--label", "run"])
    summarized = [u for u in FakeEngine.users if "Band:" in u or "Importance:" in u]
    assert assemble_brief.main(["--run", "run", "--digests-dir", str(digests), "--config", str(config_path),
                                "--no-summaries"]) == 0
    brief = next(digests.glob("*-brief.md")).read_text(encoding="utf-8")
    manifest = json.loads((digests / ".runs" / "run" / "run.json").read_text(encoding="utf-8"))
    return pitch, brief, manifest, summarized


def test_an_ad_of_a_marked_account_is_neither_summarized_nor_shown_and_the_brief_links_it(tmp_path, monkeypatch, capsys):
    pitch, brief, manifest, summarized = _run(tmp_path, monkeypatch, "unusual_whales | ads:off\ndave\n")
    out = capsys.readouterr().out
    assert "ad check: 2 posts from the 1 account marked ads:off" in out
    assert "ads: 1 of 2 posts hidden" in out
    assert "ads: 1 hidden, linked at the end of the brief" in out
    assert manifest["ads"]["hidden"] == [pitch.id]
    assert summarized and not any("your own console" in u for u in summarized)
    assert "your own console" not in brief  # an image tweet is shown whole: not this one
    assert f"*Hidden as ads (`ads:off` in sources/x.md): @unusual\\_whales ([1]({pitch.url})).*" in brief
    assert "https://x.com/unusual_whales/status/2" in brief  # its news stays
    assert len(FakeEngine.ad_checks) == 1 and "@unusual_whales: BREAKING" in FakeEngine.ad_checks[0]
    assert "Globex" not in FakeEngine.ad_checks[0]  # dave is not marked: never asked about


def test_without_a_marked_account_nothing_is_checked_and_the_brief_is_as_before(tmp_path, monkeypatch, capsys):
    pitch, brief, manifest, _summarized = _run(tmp_path, monkeypatch, "unusual_whales\ndave\n")
    assert manifest["ads"] is None
    assert "ad check" not in capsys.readouterr().out
    assert "your own console" in brief
    assert "Hidden as ads" not in brief


def test_a_dry_run_says_how_many_posts_the_check_would_read_and_calls_nothing(tmp_path, monkeypatch, capsys):
    config_path, export, _pitch = _world(tmp_path, "## markets | ads:off\nunusual_whales\ndave\n")
    monkeypatch.setattr(run_system, "list_models", lambda *args: (_ for _ in ()).throw(AssertionError("no call")))
    assert run_system.main(["--export", str(export), "--digests-dir", str(tmp_path / "digests"), "--dry-run",
                            "--sources-dir", str(tmp_path / "sources"), "--config", str(config_path)]) == 0
    assert ("ad check: 3 posts from the 2 accounts marked ads:off, for the model to read on a real run"
            in capsys.readouterr().out)

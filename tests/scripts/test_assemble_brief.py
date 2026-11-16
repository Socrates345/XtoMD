import argparse
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))  # the scripts are run from a checkout, not installed

import assemble_brief  # noqa: E402
import run_system  # noqa: E402
from xmd.core.config import Language  # noqa: E402
from xmd.summary.budget import MAX_COMPRESSION, Sizing  # noqa: E402
from xmd.summary.translate import Translated, Translation  # noqa: E402

EN_FR = Language(("en", "fr"), "en")


def test_a_brief_never_replaces_an_earlier_one(tmp_path):
    first = tmp_path / "2026-09-29-8pm-24h-brief.md"
    assert assemble_brief._unused(first) == first
    first.write_text("the 30% brief", encoding="utf-8")
    second = assemble_brief._unused(first)
    assert second.name == "2026-09-29-8pm-24h-brief-2.md"
    second.write_text("the 10% brief", encoding="utf-8")
    assert assemble_brief._unused(first).name == "2026-09-29-8pm-24h-brief-3.md"
    assert first.read_text(encoding="utf-8") == "the 30% brief"


def test_a_run_whose_export_is_gone_says_so_instead_of_a_traceback(tmp_path, capsys):
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "x.md").write_text("dave\n", encoding="utf-8")
    (tmp_path / "sources.yaml").write_text("x:\n  api_key: test-key\n", encoding="utf-8")
    run = tmp_path / "digests" / ".runs" / "old-run"
    run.mkdir(parents=True)
    (run / "run.json").write_text(json.dumps({"export": {"file": "2026-09-29-1600.txt", "sha256": "0"}, "chunks": []}),
                                  encoding="utf-8")

    code = assemble_brief.main(["--run", "old-run", "--digests-dir", str(tmp_path / "digests"),
                                "--config", str(tmp_path / "sources.yaml")])
    assert code == 1
    assert "2026-09-29-1600.txt or its .map.json is missing" in capsys.readouterr().out


def test_the_calls_made_here_go_to_the_runs_own_server_with_the_runs_own_extra_body():
    args = argparse.Namespace(model="", base_url="", api_key="")
    venice = {"venice_parameters": {"disable_thinking": True}}
    run_engine = {"model": "qwen3-5-35b-a3b", "base_url": "https://api.venice.ai/api/v1", "extra_body": venice}
    with assemble_brief._engine(args, run_engine) as engine:
        assert (engine.model, engine.base_url, engine.extra_body) == ("qwen3-5-35b-a3b", run_engine["base_url"], venice)
    with assemble_brief._engine(args, {"model": "m", "base_url": "http://127.0.0.1:1234/v1"}) as engine:
        assert engine.extra_body == {}  # a run made before there was one


def test_a_share_kept_and_a_compression_are_one_or_the_other(capsys):
    with pytest.raises(SystemExit):
        run_system.main(["--keep", "30", "--compression", "0.1", "--dry-run"])
    assert "not allowed with argument" in capsys.readouterr().err
    with pytest.raises(argparse.ArgumentTypeError, match="not a percentage"):
        run_system.keep_share("ten")
    with pytest.raises(SystemExit):
        run_system.main(["--time", "10", "--dry-run"])  # minutes are gone: said so, not taken for --timeout
    assert "use --keep PCT" in capsys.readouterr().err
    with pytest.raises(argparse.ArgumentTypeError, match="not JSON"):
        run_system.extra_body("{thinking: off}")


def test_the_translations_are_reported_in_one_line_with_the_number_really_translated():
    german = {f"Beitrag {k}": Translation("de", f"post {k}") for k in range(9)}
    done = Translated({**german, "今天": Translation("zh", "today"), "hoje": Translation("pt", "today")}, 118, 11)
    assert assemble_brief._translation_line(done, EN_FR) == (
        "translations: 11 posts translated into en (9 German, 1 Chinese, 1 Portuguese)")
    assert assemble_brief._translation_line(Translated(checked=118), EN_FR) == "translations: none needed"
    left = Translated({"hoje": Translation("pt", "today")}, 5, 3, ["a German post: HTTP 500"], too_long=1)
    assert assemble_brief._translation_line(left, EN_FR) == (
        "translations: 1 post translated into en (1 Portuguese), 1 too long to translate")
    assert assemble_brief._translation_line(Translated(failed=["language tags for 25 posts: HTTP 500"]), EN_FR) == (
        "translations: 0 posts translated into en")  # not "none needed": nobody knows, and the failure is printed under it


# the brief without its summaries, uncut and at its barest, as brief_length weighs them (the old minutes x 40)
WHOLE = {"length": 820, "band_lengths": {"priority": 80, "images": 680, "trending": 40}}
BAREST = {"length": 140, "band_lengths": {"priority": 80, "images": 8, "trending": 40}}


def test_a_share_kept_says_how_it_is_spent():
    lines = run_system._sizing_lines(40, 1000, 400, Sizing(0.21, 1060, 200, 200), False, WHOLE, BAREST)
    assert lines[0] == "keep 40%: the whole day weighs ~1000 lines (pictures counted as lines), so ~400 to keep. So:"
    assert (f"summaries        ~200 at most (20% of the day), compression 0.21 (lowered from {MAX_COMPRESSION:.2f} "
            "to fit half of it)") in lines[1]
    assert "priority tweets  ~80, in full" in lines[2]
    assert "image tweets     ~60 of the ~680 they take whole" in lines[3]  # 400 - 140 - 200
    assert "longer than asked" not in "\n".join(lines)


def test_a_share_the_whole_brief_fits_cuts_nothing_and_says_so_in_one_line():
    lines = run_system._sizing_lines(100, 820, 820, Sizing(MAX_COMPRESSION, 820, 0, 820), False, WHOLE, BAREST)
    assert lines == ["keep 100%: the whole day weighs ~820 lines (pictures counted as lines), so ~820 to keep; "
                     f"the whole brief fits, so nothing is cut (compression {MAX_COMPRESSION:.2f})"]


def test_a_share_kept_keeps_the_usual_compression_when_only_the_image_tweets_have_to_give():
    lines = run_system._sizing_lines(75, 1000, 750, Sizing(MAX_COMPRESSION, 1060, 228, 375), False, WHOLE, BAREST)
    assert f"compression {MAX_COMPRESSION:.2f} (the usual one: they have the room)" in lines[1]
    assert "image tweets     ~382 of the ~680" in lines[3]


def test_a_share_too_small_for_the_digest_says_the_brief_may_be_longer():
    barest = {"length": 88, "band_lengths": {"priority": 28, "images": 8, "trending": 40}}
    lines = run_system._sizing_lines(20, 1000, 200, Sizing(0.10, 1060, 128, 100), True, WHOLE, barest)
    assert "compression 0.10 (the lowest there is, though more than half of it)" in lines[1]
    assert "one line each (in full ~80: too long for the brief to fit)" in lines[2]
    assert "image tweets     ~0 of the ~680" in lines[3]
    assert lines[4] == "  that is more than 20% of the day: the brief may come out longer than asked"

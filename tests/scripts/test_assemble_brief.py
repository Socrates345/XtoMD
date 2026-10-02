import argparse
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))  # the scripts are run from a checkout, not installed

import assemble_brief  # noqa: E402
import run_system  # noqa: E402
from xmd.summary.budget import Sizing  # noqa: E402


def test_a_brief_never_replaces_an_earlier_one(tmp_path):
    first = tmp_path / "2026-09-29-8pm-24h-brief.md"
    assert assemble_brief._unused(first) == first
    first.write_text("the 30% brief", encoding="utf-8")
    second = assemble_brief._unused(first)
    assert second.name == "2026-09-29-8pm-24h-brief-2.md"
    second.write_text("the 10% brief", encoding="utf-8")
    assert assemble_brief._unused(first).name == "2026-09-29-8pm-24h-brief-3.md"
    assert first.read_text(encoding="utf-8") == "the 30% brief"


def test_time_and_compression_are_one_or_the_other(capsys):
    with pytest.raises(SystemExit):
        run_system.main(["--time", "10", "--compression", "0.1", "--dry-run"])
    assert "not allowed with argument" in capsys.readouterr().err
    with pytest.raises(argparse.ArgumentTypeError, match="number of minutes"):
        run_system.time_budget("ten")


# the brief without its summaries, uncut and at its barest: 2 minutes of priority tweets, 17 of image tweets
WHOLE = {"minutes_at_your_pace": 20.5, "band_minutes": {"priority": 2.0, "images": 17.0, "trending": 1.0}}
BAREST = {"minutes_at_your_pace": 3.5, "band_minutes": {"priority": 2.0, "images": 0.2, "trending": 1.0}}


def test_a_reading_time_says_how_the_time_is_shared_at_the_readers_pace():
    lines = run_system._sizing_lines(10, Sizing(0.21, 26.5, 5.0, 5.0), False, WHOLE, BAREST)
    assert lines[0] == "time 10 min (40 lines a minute, 4 s a picture): uncut, this brief would take ~26 min. So:"
    assert "summaries        ~5.0 min at most, compression 0.21 (lowered from 0.30 to fit half the time)" in lines[1]
    assert "priority tweets  ~2.0 min, in full" in lines[2]
    assert "image tweets     ~1.5 min of the ~17.0 they take whole" in lines[3]  # 10 - 3.5 - 5.0
    assert "longer than asked" not in "\n".join(lines)


def test_a_reading_time_the_whole_brief_fits_cuts_nothing_and_says_so_in_one_line():
    lines = run_system._sizing_lines(30, Sizing(0.30, 26.5, 5.7, 30), False, WHOLE, BAREST)
    assert lines == ["time 30 min (40 lines a minute, 4 s a picture): the whole brief takes ~26 min at most, "
                     "so nothing is cut (compression 0.30)"]


def test_a_reading_time_keeps_the_usual_compression_when_only_the_image_tweets_have_to_give():
    lines = run_system._sizing_lines(20, Sizing(0.30, 26.5, 5.7, 10), False, WHOLE, BAREST)
    assert "compression 0.30 (the usual one: they have the room)" in lines[1]
    assert "image tweets     ~10.8 min of the ~17.0" in lines[3]


def test_a_reading_time_too_short_for_the_digest_says_the_brief_may_be_longer():
    barest = {"minutes_at_your_pace": 2.2, "band_minutes": {"priority": 0.7, "images": 0.2, "trending": 1.0}}
    lines = run_system._sizing_lines(5, Sizing(0.10, 26.5, 3.2, 2.5), True, WHOLE, barest)
    assert "compression 0.10 (the lowest there is, though more than half the time)" in lines[1]
    assert "one line each (in full ~2.0 min: too long for the brief to fit)" in lines[2]
    assert "image tweets     ~0.0 min" in lines[3]
    assert lines[4] == "  that is more than 5 min: the brief may come out longer than asked"

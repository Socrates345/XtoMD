import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))  # the scripts are run from a checkout, not installed

import assemble_brief  # noqa: E402
import freeze_export  # noqa: E402

BROKEN = 'x:\n  backend: [twitterapi\n  api_key: "sekret-value"\n'  # a syntax error with the key on the next line


def _broken_config(tmp_path) -> str:
    path = tmp_path / "sources.yaml"
    path.write_text(BROKEN, encoding="utf-8")
    return str(path)


def _is_a_clean_one_line_error(stopped: pytest.ExceptionInfo) -> bool:
    message = str(stopped.value)  # sys.exit("...") carries the text as the exit code
    return message.startswith("config error:") and "not valid YAML" in message and "sekret-value" not in message


def test_assemble_brief_reports_a_broken_config_in_one_line_before_it_reads_anything(tmp_path):
    with pytest.raises(SystemExit) as stopped:
        assemble_brief.main(["--config", _broken_config(tmp_path), "--digests-dir", str(tmp_path)])
    assert _is_a_clean_one_line_error(stopped)


def test_freeze_export_reports_a_broken_config_in_one_line(tmp_path):
    with pytest.raises(SystemExit) as stopped:
        freeze_export.main(["--from", "2026-09-18T16:00", "--to", "2026-09-19T16:00", "--config", _broken_config(tmp_path)])
    assert _is_a_clean_one_line_error(stopped)

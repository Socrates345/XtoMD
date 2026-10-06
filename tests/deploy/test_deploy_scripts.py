"""deploy/: the entry point the VPS and the phone share (brief.sh), and all a phone's SSH key may do on the VPS
(vps/ssh-entry.sh). Each test runs a copy of deploy/ in a scratch repo whose `python` is a stand-in."""

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest

if sys.platform == "win32" or not all(shutil.which(tool) for tool in ("bash", "flock", "tar")):
    pytest.skip("the deploy scripts run on Linux and Android", allow_module_level=True)

import fcntl  # noqa: E402  (not on Windows)

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"
DAY = 86400

# .venv/bin/python: answers the one question common.sh asks of the config, and "makes a brief" otherwise
PYTHON = """#!/usr/bin/env bash
if [[ "$1" == -c ]]; then echo digests; exit 0; fi
printf '%s\\n' "$*" >> calls.log
printf '%s\\n' "${XMD_LLM_EXTRA_BODY:-}" > extra-body.log
echo "the brief" > digests/2026-10-06-5pm-24h-brief.md
exit "$(cat exit-code 2> /dev/null || echo 0)"
"""


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    shutil.copytree(DEPLOY, root / "deploy")
    python = root / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text(PYTHON, encoding="utf-8")
    python.chmod(0o755)
    (root / "digests").mkdir()
    return root


def _run(repo: Path, script: str, *args: str, **env: str) -> subprocess.CompletedProcess:
    clean = {"PATH": os.environ["PATH"], "HOME": str(repo.parent), **env}  # none of the tester's own XMD_ settings
    return subprocess.run(["bash", str(repo / "deploy" / script), *args], env=clean, capture_output=True, timeout=60)


def _aged(path: Path, days: float) -> Path:
    """`path`, made if it is not there, last touched `days` ago."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
    then = time.time() - days * DAY
    os.utime(path, (then, then))
    return path


def _calls(repo: Path) -> list[str]:
    log = repo / "calls.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


# --- brief.sh


@pytest.mark.parametrize("args, asked", [
    (["24h"], "scripts/make_brief.py --24h --wait 0"),
    (["since"], "scripts/make_brief.py --since-last-run --wait 0"),
    (["24h", "10"], "scripts/make_brief.py --24h --wait 0 --time 10"),
])
def test_a_brief_is_made_for_the_window_and_the_reading_time_asked_for_and_no_server_is_waited_for(repo, args, asked):
    assert _run(repo, "brief.sh", *args).returncode == 0
    assert _calls(repo) == [asked]


@pytest.mark.parametrize("args", [[], ["48h"], ["24h", "ten"], ["24h", "10", "more"], ["24h; id"], ["--24h"]])
def test_anything_but_a_window_and_a_number_of_minutes_is_refused_before_anything_runs(repo, args):
    done = _run(repo, "brief.sh", *args)
    assert done.returncode == 2 and b"usage" in done.stderr
    assert _calls(repo) == []


def test_the_settings_come_from_dot_env_and_a_json_value_arrives_whole(repo):
    venice = {"venice_parameters": {"disable_thinking": True, "include_venice_system_prompt": False}}
    (repo / ".env").write_text(f"XMD_LLM_EXTRA_BODY='{json.dumps(venice)}'\n", encoding="utf-8")

    assert _run(repo, "brief.sh", "24h").returncode == 0
    assert json.loads((repo / "extra-body.log").read_text(encoding="utf-8")) == venice


def test_the_example_settings_are_ones_the_shell_can_read(repo):
    shutil.copy(repo / "deploy" / "env.example", repo / ".env")

    assert _run(repo, "brief.sh", "24h").returncode == 0
    assert "disable_thinking" in json.loads((repo / "extra-body.log").read_text(encoding="utf-8"))["venice_parameters"]


def test_on_the_phone_the_brief_just_made_is_copied_where_it_can_be_read_and_no_older_one(repo, tmp_path):
    _aged(repo / "digests" / "2026-10-05-6pm-24h-brief.md", 1)
    dest = tmp_path / "shared" / "briefs"

    done = _run(repo, "brief.sh", "24h", XMD_BRIEF_DEST=str(dest))

    assert done.returncode == 0
    assert [p.name for p in dest.iterdir()] == ["2026-10-06-5pm-24h-brief.md"]
    assert b"copied 2026-10-06-5pm-24h-brief.md" in done.stdout


def test_a_brief_with_failed_chunks_is_still_copied_and_the_exit_code_says_so(repo, tmp_path):
    (repo / "exit-code").write_text("1", encoding="utf-8")
    dest = tmp_path / "briefs"

    assert _run(repo, "brief.sh", "24h", XMD_BRIEF_DEST=str(dest)).returncode == 1
    assert (dest / "2026-10-06-5pm-24h-brief.md").exists()


def test_two_briefs_are_never_made_at_once(repo):
    with open(repo / "digests" / ".brief.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # the timer's run, say
        done = _run(repo, "brief.sh", "24h")
    assert done.returncode == 75 and b"already being made" in done.stderr
    assert _calls(repo) == []
    assert _run(repo, "brief.sh", "24h").returncode == 0  # and once it is over, the next one runs


def test_what_is_older_than_the_days_to_keep_is_deleted_and_nothing_is_without_that_setting(repo):
    digests = repo / "digests"
    old = [_aged(digests / "2026-09-10-6pm-24h-brief.md", 20), _aged(digests / ".export" / "2026-09-10-1600.txt", 20),
           _aged(digests / ".runs" / "old-run" / "run.json", 20)]
    _aged(digests / ".runs" / "old-run", 20)
    kept = [_aged(digests / "2026-10-01-6pm-24h-brief.md", 5), _aged(digests / ".export" / "2026-10-01-1600.txt", 5),
            _aged(digests / ".runs" / "new-run" / "run.json", 5), _aged(digests / "archive.7z", 20)]

    assert _run(repo, "brief.sh", "24h").returncode == 0
    assert all(p.exists() for p in old + kept)

    assert _run(repo, "brief.sh", "24h", XMD_KEEP_DAYS="14").returncode == 0
    assert not any(p.exists() for p in old) and not (digests / ".runs" / "old-run").exists()
    assert all(p.exists() for p in kept)


# --- termux/phone.sh, the brief made on the phone alone


def test_a_brief_made_on_the_phone_goes_to_documents_unless_dot_env_names_another_folder(repo, tmp_path):
    home = repo.parent  # what _run gives the scripts as $HOME

    assert _run(repo, "termux/phone.sh", "24h", "10").returncode == 0
    assert _calls(repo) == ["scripts/make_brief.py --24h --wait 0 --time 10"]
    assert (home / "storage" / "shared" / "Documents" / "xmd-briefs" / "2026-10-06-5pm-24h-brief.md").exists()

    (repo / "digests" / "2026-10-06-5pm-24h-brief.md").unlink()  # the stand-in writes the same name every time
    (repo / ".env").write_text(f'XMD_BRIEF_DEST="{tmp_path}/vault/Briefs"\n', encoding="utf-8")
    assert _run(repo, "termux/phone.sh", "24h").returncode == 0
    assert (tmp_path / "vault" / "Briefs" / "2026-10-06-5pm-24h-brief.md").exists()


# --- vps/ssh-entry.sh


@pytest.fixture
def vps(repo):
    """The scratch repo with brief.sh replaced by one that only says what it was asked."""
    (repo / "deploy" / "brief.sh").write_text('printf "%s\\n" "$*" >> "$(dirname "$0")/../asked.log"\n', encoding="utf-8")
    return repo


def _ssh(repo: Path, request: str) -> subprocess.CompletedProcess:
    return _run(repo, "vps/ssh-entry.sh", SSH_ORIGINAL_COMMAND=request)


def _asked(repo: Path) -> list[str]:
    log = repo / "asked.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


@pytest.mark.parametrize("request_, passed_on", [("run 24h", "24h"), ("run since", "since"), ("run 24h 10", "24h 10"),
                                                 ("  run   since   30 ", "since 30")])
def test_the_phone_may_ask_for_a_brief(vps, request_, passed_on):
    assert _ssh(vps, request_).returncode == 0
    assert _asked(vps) == [passed_on]


@pytest.mark.parametrize("request_", [
    "", "ls", "bash", "bash -c id", "scp -t /", "rsync --server --sender . /", "cat sources.yaml",
    "run", "run 48h", "run 24h ten", "run 24h 10 more", "run 24h --time", "run 24h 1e3", "run 24h 1000",
    "run 24h;id", "run 24h 10;id", "run 24h $(id)", "run 24h `id`", "run 24h 10 && id", "run 24h | id", "run 24h\nid",
    "pull ..", "pull ../..", "pull 1 2", "pull -1", "pull 100", "pull /etc", "pull 1;id",
])
def test_the_phone_may_ask_for_nothing_else(vps, request_):
    _aged(vps / "digests" / "2026-10-06-5pm-24h-brief.md", 0)
    done = _ssh(vps, request_)
    assert done.returncode == 2 and b"allowed:" in done.stderr
    assert done.stdout == b"" and _asked(vps) == []


def _names(archive: bytes) -> list[str]:
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        return sorted(tar.getnames())


def test_pull_sends_the_recent_briefs_and_only_briefs(vps):
    digests = vps / "digests"
    _aged(digests / "2026-10-06-5pm-24h-brief.md", 0.1)
    _aged(digests / "2026-10-06-6pm-24h-10min-brief-2.md", 0.1)
    _aged(digests / "2026-10-01-6pm-24h-brief.md", 5)
    _aged(digests / "2026-10-06-1500.md", 0.1)  # a full digest
    _aged(digests / ".runs" / "a-run" / "run.json", 0.1)
    _aged(digests / ".export" / "2026-10-06-1500.txt", 0.1)

    day = _ssh(vps, "pull")
    assert day.returncode == 0
    assert _names(day.stdout) == ["2026-10-06-5pm-24h-brief.md", "2026-10-06-6pm-24h-10min-brief-2.md"]
    assert len(_names(_ssh(vps, "pull 7").stdout)) == 3


def test_pull_with_nothing_recent_is_an_empty_archive_not_an_error(vps):
    _aged(vps / "digests" / "2026-10-01-6pm-24h-brief.md", 5)

    done = _ssh(vps, "pull")
    assert done.returncode == 0 and _names(done.stdout) == []


# --- termux/vps.sh, the phone's side of the same conversation

# `ssh HOST REQUEST...`: sshd's forced command, without the network
SSH = """#!/usr/bin/env bash
echo "$1" >> "$VPS/hosts.log"
shift
SSH_ORIGINAL_COMMAND="$*" exec bash "$VPS/deploy/vps/ssh-entry.sh"
"""


def _phone(repo: Path, tmp_path: Path, *args: str, **env: str) -> subprocess.CompletedProcess:
    ssh = tmp_path / "bin" / "ssh"
    if not ssh.exists():
        ssh.parent.mkdir()
        ssh.write_text(SSH, encoding="utf-8")
        ssh.chmod(0o755)
    path = f"{ssh.parent}{os.pathsep}{os.environ['PATH']}"
    return _run(repo, "termux/vps.sh", *args, PATH=path, VPS=str(repo), **env)


def test_from_the_phone_a_brief_is_made_on_the_vps_and_lands_in_the_phones_folder(repo, tmp_path):
    dest = tmp_path / "shared" / "briefs"

    done = _phone(repo, tmp_path, "run", "24h", "10", XMD_BRIEF_DEST=str(dest), XMD_VPS_HOST="my-vps")

    assert done.returncode == 0
    assert _calls(repo) == ["scripts/make_brief.py --24h --wait 0 --time 10"]
    assert (dest / "2026-10-06-5pm-24h-brief.md").read_text(encoding="utf-8") == "the brief\n"
    assert set((repo / "hosts.log").read_text(encoding="utf-8").split()) == {"my-vps"}


def test_a_download_leaves_a_brief_already_on_the_phone_as_it_is(repo, tmp_path):
    _aged(repo / "digests" / "2026-10-06-5pm-24h-brief.md", 0.1)
    _aged(repo / "digests" / "2026-10-06-9pm-since-brief.md", 0.1)
    dest = tmp_path / "briefs"
    dest.mkdir()
    (dest / "2026-10-06-5pm-24h-brief.md").write_text("read, with my notes", encoding="utf-8")

    assert _phone(repo, tmp_path, "pull", XMD_BRIEF_DEST=str(dest)).returncode == 0

    assert sorted(p.name for p in dest.iterdir()) == ["2026-10-06-5pm-24h-brief.md", "2026-10-06-9pm-since-brief.md"]
    assert (dest / "2026-10-06-5pm-24h-brief.md").read_text(encoding="utf-8") == "read, with my notes"


def test_the_phone_says_how_to_ask_when_asked_for_something_else(repo, tmp_path):
    done = _phone(repo, tmp_path, "status")
    assert done.returncode == 2 and b"usage" in done.stderr
    assert not (repo / "hosts.log").exists()  # the VPS was not even called

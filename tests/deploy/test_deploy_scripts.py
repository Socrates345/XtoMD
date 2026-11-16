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

if sys.platform == "win32" or not all(shutil.which(tool) for tool in ("bash", "flock", "setsid", "tar")):
    pytest.skip("the deploy scripts run on Linux and Android", allow_module_level=True)

import fcntl  # noqa: E402  (not on Windows)

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"
DAY = 86400

# .venv/bin/python: answers the one question common.sh asks of the config, and "makes a brief" otherwise (unless
# there is a no-brief file: a run that stopped before its brief, or had nothing new)
PYTHON = """#!/usr/bin/env bash
if [[ "$1" == -c ]]; then echo digests; exit 0; fi
printf '%s\\n' "$*" >> calls.log
printf '%s\\n' "${XMD_LLM_EXTRA_BODY:-}" > extra-body.log
[[ -f no-brief ]] || echo "the brief" > digests/2026-10-06-5pm-24h-brief.md
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
    (["24h", "30"], "scripts/make_brief.py --24h --wait 0 --keep 30"),
    (["since", "30%"], "scripts/make_brief.py --since-last-run --wait 0 --keep 30"),
    (["24h", "100"], "scripts/make_brief.py --24h --wait 0 --keep 100"),
    (["24h", "007"], "scripts/make_brief.py --24h --wait 0 --keep 7"),
])
def test_a_brief_is_made_for_the_window_and_the_share_asked_for_and_no_server_is_waited_for(repo, args, asked):
    assert _run(repo, "brief.sh", *args).returncode == 0
    assert _calls(repo) == [asked]


@pytest.mark.parametrize("args", [[], ["48h"], ["24h", "ten"], ["24h", "30", "more"], ["24h; id"], ["--24h"],
                                  ["24h", "0"], ["24h", "101"], ["24h", "10min"], ["24h", "%"], ["24h", ""]])
def test_anything_but_a_window_and_a_percentage_is_refused_before_anything_runs(repo, args):
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


def test_the_briefs_older_than_the_days_to_keep_are_deleted_and_nothing_is_without_that_setting(repo):
    digests = repo / "digests"
    old = _aged(digests / "2026-09-10-6pm-24h-brief.md", 20)
    # old exports and runs are the fetch's to delete (EXPORT_KEEP_DAYS in xmd/cli.py), not this setting's
    kept = [_aged(digests / "2026-10-01-6pm-24h-brief.md", 5), _aged(digests / ".export" / "2026-09-10-1600.txt", 20),
            _aged(digests / ".runs" / "old-run" / "run.json", 20), _aged(digests / "archive.7z", 20)]

    assert _run(repo, "brief.sh", "24h").returncode == 0
    assert old.exists() and all(p.exists() for p in kept)

    assert _run(repo, "brief.sh", "24h", XMD_KEEP_DAYS="14").returncode == 0
    assert not old.exists() and all(p.exists() for p in kept)


# --- brief.sh's notification, on the ntfy topic XMD_NOTIFY_URL names

TOPIC = "https://ntfy.sh/xmd-0123456789abcdef"

# `curl ...`: each call recorded in a folder of its own, its arguments and what came on its stdin, and nothing sent
CURL = """#!/usr/bin/env bash
call="$NOTIFIED/$(ls "$NOTIFIED" | wc -l)"
mkdir "$call"
printf '%s\\0' "$@" > "$call/args"
cat > "$call/stdin"
exit "$(cat "$NOTIFIED/../curl-exit-code" 2> /dev/null || echo 0)"
"""


def _notifying(repo: Path, tmp_path: Path, *args: str, topic: str | None = TOPIC) -> subprocess.CompletedProcess:
    """brief.sh, `topic` in its environment, and a curl on its PATH that only records what it was asked to send."""
    curl = tmp_path / "bin" / "curl"
    if not curl.exists():
        curl.parent.mkdir()
        curl.write_text(CURL, encoding="utf-8")
        curl.chmod(0o755)
    (repo / "notified").mkdir(exist_ok=True)
    env = {"PATH": f"{curl.parent}{os.pathsep}{os.environ['PATH']}", "NOTIFIED": str(repo / "notified")}
    return _run(repo, "brief.sh", *args, **env, **({"XMD_NOTIFY_URL": topic} if topic else {}))


def _notifications(repo: Path) -> list[dict[str, str]]:
    """What curl was asked to send, call by call: its headers, its body, its whole command line and its stdin."""
    calls = repo / "notified"
    sent = []
    for call in sorted(calls.iterdir(), key=lambda p: int(p.name)) if calls.exists() else []:
        args = (call / "args").read_text(encoding="utf-8").split("\0")[:-1]
        headers = dict(args[i + 1].split(": ", 1) for i, arg in enumerate(args) if arg == "-H")
        sent.append({**headers, "body": args[args.index("--data-raw") + 1], "argv": " ".join(args),
                     "stdin": (call / "stdin").read_text(encoding="utf-8")})
    return sent


def test_a_new_brief_is_announced_by_its_name_and_no_older_one_is(repo, tmp_path):
    _aged(repo / "digests" / "2026-10-05-6pm-24h-brief.md", 1)

    assert _notifying(repo, tmp_path, "24h").returncode == 0

    [sent] = _notifications(repo)
    assert sent["Title"] == "XtoMD: brief ready" and sent["Tags"] == "newspaper"
    assert sent["body"] == "2026-10-06-5pm-24h-brief.md"


def test_the_topic_comes_from_dot_env_and_reaches_curl_on_stdin_not_on_a_command_line(repo, tmp_path):
    (repo / ".env").write_text(f"XMD_NOTIFY_URL={TOPIC}\n", encoding="utf-8")

    assert _notifying(repo, tmp_path, "24h", topic=None).returncode == 0

    [sent] = _notifications(repo)
    assert sent["stdin"] == f'url = "{TOPIC}"\n'
    assert "ntfy.sh" not in sent["argv"]


def test_without_a_topic_nothing_is_sent(repo, tmp_path):
    assert _notifying(repo, tmp_path, "24h", topic=None).returncode == 0
    assert _notifications(repo) == []


def test_a_brief_with_failed_chunks_is_announced_as_such_and_the_exit_code_still_says_so(repo, tmp_path):
    (repo / "exit-code").write_text("1", encoding="utf-8")

    assert _notifying(repo, tmp_path, "24h").returncode == 1

    [sent] = _notifications(repo)
    assert sent["Title"] == "XtoMD: brief ready, some posts not summarized"
    assert sent["body"] == "2026-10-06-5pm-24h-brief.md"


def test_a_run_that_ends_without_its_brief_says_it_failed_and_one_with_nothing_new_says_nothing(repo, tmp_path):
    (repo / "no-brief").touch()
    (repo / "exit-code").write_text("1", encoding="utf-8")  # the model server went away, say

    assert _notifying(repo, tmp_path, "24h").returncode == 1
    [failed] = _notifications(repo)
    assert failed["Title"] == "XtoMD: no brief, the run failed" and failed["Tags"] == "warning"
    assert failed["body"].startswith("exit 1: ")

    (repo / "exit-code").write_text("0", encoding="utf-8")  # nothing new in the window
    assert _notifying(repo, tmp_path, "24h").returncode == 0
    assert len(_notifications(repo)) == 1  # the failure's, still alone


def test_a_notification_that_cannot_be_sent_fails_nothing_else(repo, tmp_path):
    (repo / "curl-exit-code").write_text("6", encoding="utf-8")  # curl's "could not resolve host"

    done = _notifying(repo, tmp_path, "24h")

    assert done.returncode == 0
    assert b"the notification could not be sent" in done.stderr
    assert len(_notifications(repo)) == 1


# --- termux/phone.sh, the brief made on the phone alone


def test_a_brief_made_on_the_phone_goes_to_documents_unless_dot_env_names_another_folder(repo, tmp_path):
    home = repo.parent  # what _run gives the scripts as $HOME

    assert _run(repo, "termux/phone.sh", "24h", "30").returncode == 0
    assert _calls(repo) == ["scripts/make_brief.py --24h --wait 0 --keep 30"]
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


@pytest.mark.parametrize("request_, passed_on", [("run 24h", "24h"), ("run since", "since"), ("run 24h 30", "24h 30"),
                                                 ("  run   since   30 ", "since 30")])
def test_the_phone_may_ask_for_a_brief(vps, request_, passed_on):
    assert _ssh(vps, request_).returncode == 0
    assert _asked(vps) == [passed_on]


def test_what_the_brief_prints_and_how_it_ends_reach_the_phone_and_nothing_is_left_behind(vps):
    (vps / "deploy" / "brief.sh").write_text("echo made; echo 'already being made' >&2; exit 75\n", encoding="utf-8")

    done = _ssh(vps, "run 24h")

    assert done.returncode == 75
    assert done.stdout.splitlines() == [b"made", b"already being made"]  # one stream: the order a terminal shows
    assert [p.name for p in (vps / "digests").iterdir()] == []  # the file the output went through is gone


def test_a_brief_asked_for_from_the_phone_is_finished_even_if_the_phone_goes_away(vps):
    (vps / "deploy" / "brief.sh").write_text(
        'echo started\nsleep 1\nprintf "%s\\n" "$*" >> "$(dirname "$0")/../asked.log"\n', encoding="utf-8")
    env = {"PATH": os.environ["PATH"], "HOME": str(vps.parent), "SSH_ORIGINAL_COMMAND": "run 24h 30"}
    session = subprocess.Popen(["bash", str(vps / "deploy" / "vps" / "ssh-entry.sh")], env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    assert session.stdout.readline() == b"started\n"
    session.stdout.close()  # the connection is lost ...
    session.kill()  # ... and sshd takes what it started down with it
    session.wait()
    assert _asked(vps) == []  # the brief was still in the middle

    deadline = time.time() + 10
    while not _asked(vps) and time.time() < deadline:
        time.sleep(0.1)
    assert _asked(vps) == ["24h 30"]


@pytest.mark.parametrize("request_", [
    "", "ls", "bash", "bash -c id", "scp -t /", "rsync --server --sender . /", "cat sources.yaml",
    "run", "run 48h", "run 24h ten", "run 24h 10 more", "run 24h --keep", "run 24h 30%", "run 24h 1e3", "run 24h 1000",
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
    _aged(digests / "2026-10-06-6pm-24h-keep30-brief-2.md", 0.1)
    _aged(digests / "2026-10-01-6pm-24h-brief.md", 5)
    _aged(digests / "2026-10-06-1500.md", 0.1)  # a full digest
    _aged(digests / ".runs" / "a-run" / "run.json", 0.1)
    _aged(digests / ".export" / "2026-10-06-1500.txt", 0.1)

    day = _ssh(vps, "pull")
    assert day.returncode == 0
    assert _names(day.stdout) == ["2026-10-06-5pm-24h-brief.md", "2026-10-06-6pm-24h-keep30-brief-2.md"]
    assert len(_names(_ssh(vps, "pull 7").stdout)) == 3


def test_pull_with_nothing_recent_is_an_empty_archive_not_an_error(vps):
    _aged(vps / "digests" / "2026-10-01-6pm-24h-brief.md", 5)

    done = _ssh(vps, "pull")
    assert done.returncode == 0 and _names(done.stdout) == []


# --- termux/vps.sh, the phone's side of the same conversation

# `ssh HOST REQUEST...`: sshd's forced command, without the network
SSH = """#!/usr/bin/env bash
while [[ "$1" == -o ]]; do
    echo "$2" >> "$VPS/options.log"
    shift 2
done
echo "$1" >> "$VPS/hosts.log"
shift
if [[ -n "${SSH_FAILS:-}" ]]; then exit 255; fi
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

    done = _phone(repo, tmp_path, "run", "24h", "30", XMD_BRIEF_DEST=str(dest), XMD_VPS_HOST="my-vps")

    assert done.returncode == 0
    assert _calls(repo) == ["scripts/make_brief.py --24h --wait 0 --keep 30"]
    assert (dest / "2026-10-06-5pm-24h-brief.md").read_text(encoding="utf-8") == "the brief\n"
    assert set((repo / "hosts.log").read_text(encoding="utf-8").split()) == {"my-vps"}


def test_a_run_keeps_its_connection_alive_and_a_lost_one_is_explained_not_followed_by_a_download(repo, tmp_path):
    dest = tmp_path / "briefs"

    done = _phone(repo, tmp_path, "run", "24h", XMD_BRIEF_DEST=str(dest), SSH_FAILS="1")

    assert done.returncode == 255
    assert b"finished there all" in done.stderr and b"download-latest-daily-brief" in done.stderr
    assert (repo / "options.log").read_text(encoding="utf-8").split() == ["ServerAliveInterval=30", "ServerAliveCountMax=4"]
    assert (repo / "hosts.log").read_text(encoding="utf-8").split() == ["xmd-vps"]  # one call: no download tried
    assert not dest.exists()


def test_a_download_leaves_a_brief_already_on_the_phone_as_it_is(repo, tmp_path):
    _aged(repo / "digests" / "2026-10-06-5pm-24h-brief.md", 0.1)
    _aged(repo / "digests" / "2026-10-06-9pm-since-brief.md", 0.1)
    dest = tmp_path / "briefs"
    dest.mkdir()
    (dest / "2026-10-06-5pm-24h-brief.md").write_text("read, with my notes", encoding="utf-8")

    assert _phone(repo, tmp_path, "pull", XMD_BRIEF_DEST=str(dest)).returncode == 0

    assert sorted(p.name for p in dest.iterdir()) == ["2026-10-06-5pm-24h-brief.md", "2026-10-06-9pm-since-brief.md"]
    assert (dest / "2026-10-06-5pm-24h-brief.md").read_text(encoding="utf-8") == "read, with my notes"


def test_a_brief_moved_on_from_the_download_folder_is_not_downloaded_again(repo, tmp_path):
    _aged(repo / "digests" / "2026-10-06-5pm-24h-brief.md", 0.1)
    dest = tmp_path / "vault" / "_sync_playground" / "1_unverified"

    first = _phone(repo, tmp_path, "pull", XMD_BRIEF_DEST=str(dest))
    assert first.returncode == 0 and b"new: 2026-10-06-5pm-24h-brief.md" in first.stdout
    (dest / "2026-10-06-5pm-24h-brief.md").rename(tmp_path / "verified.md")  # verify.sh moved it on
    _aged(repo / "digests" / "2026-10-06-9pm-since-brief.md", 0.1)

    second = _phone(repo, tmp_path, "pull", XMD_BRIEF_DEST=str(dest))

    assert second.returncode == 0 and b"already downloaded: 2026-10-06-5pm-24h-brief.md" in second.stdout
    assert [p.name for p in dest.iterdir()] == ["2026-10-06-9pm-since-brief.md"]  # only what is new
    record = repo.parent / ".xmd-pulled"  # in Termux's home, outside the vault
    assert record.read_text(encoding="utf-8").split() == ["2026-10-06-5pm-24h-brief.md", "2026-10-06-9pm-since-brief.md"]
    assert not list(repo.parent.glob(".xmd-pull.*"))  # the staging folder is gone


def test_the_phone_says_how_to_ask_when_asked_for_something_else(repo, tmp_path):
    done = _phone(repo, tmp_path, "status")
    assert done.returncode == 2 and b"usage" in done.stderr
    assert not (repo / "hosts.log").exists()  # the VPS was not even called


# --- termux/setup.sh, the home-screen buttons

# the names setup.sh gave its buttons before: left in place, the widget would list them for ever
FORMER_BUTTONS = ["brief-vps-24h", "brief-vps-10min", "brief-vps-latest", "brief-phone-24h", "brief-phone-10min",
                  "generate-last-24h-brief-10min-read"]


def _setup(repo: Path, tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    """setup.sh in a stand-in Termux: access to the phone's files is given already, and `pkg` and `pip` install
    nothing, only write what they were asked to pkg.log and pip.log."""
    (repo.parent / "storage" / "shared").mkdir(parents=True, exist_ok=True)  # repo.parent: _run's $HOME
    prefix = tmp_path / "usr"
    for tool in (prefix / "bin" / "pkg", repo / ".venv" / "bin" / "pip"):
        tool.parent.mkdir(parents=True, exist_ok=True)
        tool.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{repo}/{tool.name}.log"\n', encoding="utf-8")
        tool.chmod(0o755)
    path = f"{prefix / 'bin'}{os.pathsep}{os.environ['PATH']}"
    return _run(repo, "termux/setup.sh", *args, PREFIX=str(prefix), PATH=path)


def _log(repo: Path, name: str) -> list[str]:
    log = repo / f"{name}.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def test_setup_leaves_five_buttons_and_clears_its_former_ones_and_no_other(repo, tmp_path):
    shortcuts = repo.parent / ".shortcuts"
    shortcuts.mkdir()
    for name in [*FORMER_BUTTONS, "my-own-button"]:
        (shortcuts / name).write_text("echo mine\n", encoding="utf-8")
    asked = 'printf "%s\\n" "$*" >> "$(dirname "$0")/../../asked.log"\n'  # vps.sh, saying only what it was asked
    (repo / "deploy" / "termux" / "vps.sh").write_text(asked, encoding="utf-8")
    (repo / "deploy" / "termux" / "verify.sh").write_text(asked.replace('"$*"', "verify"), encoding="utf-8")

    done = _setup(repo, tmp_path)

    assert done.returncode == 0, done.stderr
    buttons = ["download-latest-daily-brief", "generate-last-24h-brief", "generate-last-24h-brief-keep30",
               "generate-since-last-brief", "verify-downloaded-briefs"]
    assert sorted(p.name for p in shortcuts.iterdir()) == sorted([*buttons, "my-own-button"])
    for button in buttons:
        subprocess.run(["bash", str(shortcuts / button)], check=True, timeout=60)
    assert _asked(repo) == ["pull", "run 24h", "run 24h 30", "run since", "verify"]


def test_setup_checks_it_can_write_where_dot_env_sends_the_briefs(repo, tmp_path):
    vault = repo.parent / "storage" / "shared" / "Obsidian" / "Vault" / "_sync_playground" / "1_unverified"
    (repo / ".env").write_text(f'XMD_BRIEF_DEST="{vault}"\n', encoding="utf-8")

    done = _setup(repo, tmp_path)

    assert done.returncode == 0, done.stderr
    assert b"ok: briefs go to Obsidian/Vault/_sync_playground/1_unverified" in done.stdout and vault.is_dir()


def test_setup_installs_no_python_and_gives_the_phone_only_its_own_settings(repo, tmp_path):
    done = _setup(repo, tmp_path)

    assert done.returncode == 0, done.stderr
    assert _log(repo, "pkg") == ["install -y openssh tar"]
    assert _log(repo, "pip") == []
    env = repo / ".env"
    assert env.read_bytes() == (repo / "deploy" / "termux" / "env.example").read_bytes()
    assert env.stat().st_mode & 0o777 == 0o600
    assert b"XMD_LLM_" not in env.read_bytes()  # no key to fill in
    assert b"setup.sh alone" in done.stdout  # but where to go for a brief made on the phone


def test_setup_alone_adds_python_and_the_model_settings_once_and_brief_sh_can_read_them(repo, tmp_path):
    assert _setup(repo, tmp_path, "alone").returncode == 0
    done = _setup(repo, tmp_path, "alone")  # again, after a `git pull` say

    assert done.returncode == 0, done.stderr
    assert _log(repo, "pkg") == ["install -y openssh tar python util-linux"] * 2
    assert _log(repo, "pip") == ["install --quiet -e . tzdata"] * 2
    settings = (repo / ".env").read_text(encoding="utf-8")
    assert settings.startswith((repo / "deploy" / "termux" / "env.example").read_text(encoding="utf-8"))
    assert settings.count("\nXMD_LLM_BASE_URL=") == 1 and "XMD_VPS_HOST" in settings
    assert "Copy to .env" not in settings  # the template's opening comment is not copied
    assert _run(repo, "brief.sh", "24h").returncode == 0
    assert "disable_thinking" in json.loads((repo / "extra-body.log").read_text(encoding="utf-8"))["venice_parameters"]


@pytest.mark.parametrize("args", [[], ["alone"]])
def test_setup_leaves_an_existing_dot_env_as_it_is(repo, tmp_path, args):
    mine = "XMD_LLM_BASE_URL=https://example.org/v1\nXMD_VPS_HOST=my-vps\n"
    (repo / ".env").write_text(mine, encoding="utf-8")

    assert _setup(repo, tmp_path, *args).returncode == 0
    assert (repo / ".env").read_text(encoding="utf-8") == mine


def test_setup_refuses_an_argument_it_does_not_know_before_installing_anything(repo, tmp_path):
    done = _setup(repo, tmp_path, "vps")

    assert done.returncode == 2 and b"usage" in done.stderr
    assert _log(repo, "pkg") == [] and not (repo / ".env").exists()


# --- termux/verify.sh, the gate between the vault's 1_unverified and 2_verified

@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "vault"
    for folder in ("_sync_playground/1_unverified", "_sync_playground/2_verified"):
        (root / folder).mkdir(parents=True)
    return root


def _verify(repo: Path, vault: Path, answer: bytes = b"\n", **env: str) -> subprocess.CompletedProcess:
    settings = {"XMD_BRIEF_DEST": str(vault / "_sync_playground" / "1_unverified"),
                "XMD_BRIEF_VERIFIED": str(vault / "_sync_playground" / "2_verified"),
                "XMD_BRIEF_REJECTED": str(vault / ".trash"), **env}
    clean = {"PATH": os.environ["PATH"], "HOME": str(repo.parent), **settings}
    shutil.copy(DEPLOY / "termux" / "verify.sh", repo / "deploy" / "termux" / "verify.sh")
    return subprocess.run(["bash", str(repo / "deploy" / "termux" / "verify.sh")], env=clean, input=answer,
                          capture_output=True, timeout=60)


def _put(vault: Path, rel: str, text: str) -> Path:
    path = vault / "_sync_playground" / "1_unverified" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _tree(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()) if root.exists() else []


def test_on_enter_clean_briefs_go_to_verified_and_the_rest_to_quarantine_under_the_same_path(repo, vault):
    _put(vault, "2026-10-06-5pm-24h-brief.md", "# X brief\n\n- a summary, $TSLA up 3%\n")
    _put(vault, "old/2026-10-05-brief.md", "# X brief\n")
    _put(vault, "2026-10-06-9pm-since-brief.md", "# X brief\n\nfine\n<SCRIPT>alert(1)</script>\n")
    _put(vault, "notes.txt", "plain text")

    done = _verify(repo, vault)

    assert done.returncode == 0, done.stdout
    out = done.stdout.decode()
    assert "OK   2026-10-06-5pm-24h-brief.md" in out and "OK   old/2026-10-05-brief.md" in out
    assert "FAIL 2026-10-06-9pm-since-brief.md - 1 forbidden-pattern match(es)" in out
    assert "line 4: pattern '<script' -> <SCRIPT>alert(1)</script>" in out  # case-insensitive, and where
    assert "FAIL notes.txt - not a .md file" in out
    assert _tree(vault / "_sync_playground" / "2_verified") == ["2026-10-06-5pm-24h-brief.md", "old/2026-10-05-brief.md"]
    assert _tree(vault / ".trash") == ["2026-10-06-9pm-since-brief.md", "notes.txt"]
    assert _tree(vault / "_sync_playground" / "1_unverified") == []
    assert "Moved to 2_verified:   2" in out and "Moved to quarantine:   2" in out


@pytest.mark.parametrize("pattern", ["<%", "dataviewjs", "$=", "<script", "<iframe", "<object", "<embed",
                                     "javascript:", "obsidian://", "file:///", "DataViewJS", "JavaScript:"])
def test_every_pattern_promote_ps1_refuses_is_refused(repo, vault, pattern):
    _put(vault, "brief.md", f"# X brief\n\n- a tweet with {pattern} in it\n")

    assert _verify(repo, vault).returncode == 0
    assert _tree(vault / ".trash") == ["brief.md"] and _tree(vault / "_sync_playground" / "2_verified") == []


def test_a_file_over_5_mb_is_refused_without_being_read(repo, vault):
    _put(vault, "big.md", "a" * (5 * 1024 * 1024 + 1))
    _put(vault, "just.md", "a" * (5 * 1024 * 1024))

    done = _verify(repo, vault)

    assert b"FAIL big.md - size 5242881 bytes exceeds the 5 MB limit" in done.stdout
    assert _tree(vault / ".trash") == ["big.md"] and _tree(vault / "_sync_playground" / "2_verified") == ["just.md"]


def test_without_enter_nothing_moves(repo, vault):
    _put(vault, "brief.md", "# X brief\n")

    done = _verify(repo, vault, answer=b"")  # end of input: as Ctrl+C, no Enter

    assert done.returncode == 0 and b"OK   brief.md" in done.stdout and b"nothing moved" in done.stdout
    assert _tree(vault / "_sync_playground" / "1_unverified") == ["brief.md"]


def test_a_name_already_in_verified_is_never_overwritten_and_stays_behind(repo, vault):
    _put(vault, "brief.md", "# X brief, new\n")
    (vault / "_sync_playground" / "2_verified" / "brief.md").write_text("read, with my notes", encoding="utf-8")

    done = _verify(repo, vault)

    assert done.returncode == 0 and b"brief.md  [name already exists at destination]" in done.stdout
    assert (vault / "_sync_playground" / "2_verified" / "brief.md").read_text(encoding="utf-8") == "read, with my notes"
    assert _tree(vault / "_sync_playground" / "1_unverified") == ["brief.md"]


def test_a_symbolic_link_stops_everything_before_any_scan(repo, vault, tmp_path):
    _put(vault, "brief.md", "# X brief\n")
    (tmp_path / "secret.md").write_text("elsewhere", encoding="utf-8")
    (vault / "_sync_playground" / "1_unverified" / "link.md").symlink_to(tmp_path / "secret.md")

    done = _verify(repo, vault)

    assert done.returncode == 1 and b"ABORT" in done.stdout and b"Scan report" not in done.stdout
    assert _tree(vault / "_sync_playground" / "2_verified") == [] and not (vault / ".trash").exists()


def test_a_missing_verified_folder_or_setting_stops_it_and_creates_nothing(repo, vault):
    _put(vault, "brief.md", "# X brief\n")
    (vault / "_sync_playground" / "2_verified").rmdir()

    done = _verify(repo, vault)
    assert done.returncode == 1 and b"2_verified does not exist" in done.stdout
    assert not (vault / "_sync_playground" / "2_verified").exists()

    done = _verify(repo, vault, XMD_BRIEF_REJECTED="")
    assert done.returncode == 1 and b"XMD_BRIEF_REJECTED is not set" in done.stdout
    assert _tree(vault / "_sync_playground" / "1_unverified") == ["brief.md"]


def test_an_empty_folder_has_nothing_to_verify(repo, vault):
    done = _verify(repo, vault)
    assert done.returncode == 0 and b"nothing to verify" in done.stdout


def test_a_brief_changed_between_the_report_and_enter_stays_behind(repo, vault):
    _put(vault, "a.md", "# X brief\n")
    changed = _put(vault, "b.md", "# X brief\n")
    shutil.copy(DEPLOY / "termux" / "verify.sh", repo / "deploy" / "termux" / "verify.sh")
    env = {"PATH": os.environ["PATH"], "HOME": str(repo.parent),
           "XMD_BRIEF_DEST": str(vault / "_sync_playground" / "1_unverified"),
           "XMD_BRIEF_VERIFIED": str(vault / "_sync_playground" / "2_verified"), "XMD_BRIEF_REJECTED": str(vault / ".trash")}
    run = subprocess.Popen(["bash", str(repo / "deploy" / "termux" / "verify.sh")], env=env,
                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    while not run.stdout.readline().startswith(b"-- Rejected"):  # the report is out: it waits for Enter now
        pass
    changed.write_text("# X brief\n<script>\n", encoding="utf-8")
    rest, _ = run.communicate(b"\n", timeout=60)

    assert run.returncode == 0 and b"b.md  [changed or gone since the report]" in rest
    assert _tree(vault / "_sync_playground" / "2_verified") == ["a.md"]
    assert _tree(vault / "_sync_playground" / "1_unverified") == ["b.md"]

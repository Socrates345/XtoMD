import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))  # make_brief imports its sibling scripts by name

import make_brief  # noqa: E402
from xmd.core.config import Config, Source  # noqa: E402
from xmd.summary.engine import EngineError  # noqa: E402


class World:
    """Stands in for LM Studio, the fetch, the digest and the two scripts, and records what make_brief asks of each."""

    def __init__(self, monkeypatch, tmp_path):
        self.calls: list[tuple] = []
        self.models = ["qwen/qwen3.5-9b", "other/model"]
        self.down_polls = 0  # looks at the server that fail before it answers
        self.warmup_error = ""
        self.digests = tmp_path / "digests"
        self.export: Path | None = self.digests / ".export" / "2026-09-21-1000.txt"
        self.run_code = 0
        self.writes_run = True
        self.runs_made = 0
        self.warmup_extra_body: dict | None = None  # what the warm-up call was told to add to its request
        for var in ("XMD_LLM_BASE_URL", "XMD_LLM_MODEL", "XMD_LLM_EXTRA_BODY"):  # the tester's own, if any
            monkeypatch.delenv(var, raising=False)
        config = Config(sources=[Source(name="@alice", type="twitterapi", handle="alice")], x_api_key="k",
                        storage=tmp_path / "t.db", digest_dir=self.digests)
        monkeypatch.setattr(make_brief, "load_config", lambda path: config)
        monkeypatch.setattr(make_brief, "list_models", self._list_models)
        monkeypatch.setattr(make_brief, "Engine", self._engine)
        monkeypatch.setattr(make_brief.cli, "main", lambda argv: self.calls.append(("fetch", argv)))
        monkeypatch.setattr(make_brief.cli, "run_digest", self._digest)
        monkeypatch.setattr(make_brief.run_system, "main", self._run_system)
        monkeypatch.setattr(make_brief.assemble_brief, "main", self._assemble)
        monkeypatch.setattr(make_brief, "POLL_SECONDS", 0)

    def _list_models(self, base_url, api_key):
        if self.down_polls:
            self.down_polls -= 1
            raise EngineError("connection refused")
        return self.models

    def _engine(self, model, base_url, api_key, timeout, extra_body=None):
        world = self
        world.warmup_extra_body = extra_body

        class Engine:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def complete(self, system, user, max_tokens):
                if world.warmup_error:
                    raise EngineError(world.warmup_error)
                world.calls.append(("warm-up", model))
                return SimpleNamespace(seconds=0.1)

        return Engine()

    def _digest(self, config, window, full=False, quick=False):
        self.calls.append(("digest", window, full, quick))
        return self.export

    def _run_system(self, argv):
        self.calls.append(("run_system", argv))
        if self.writes_run:
            self.runs_made += 1
            run = self.digests / ".runs" / f"run-{self.runs_made}"
            run.mkdir(parents=True)
            (run / "run.json").write_text("{}", encoding="utf-8")
        return self.run_code

    def _assemble(self, argv):
        self.calls.append(("assemble", argv))
        return 0

    @property
    def steps(self) -> list[str]:
        return [c[0] for c in self.calls]

    def call(self, name: str) -> tuple:
        return next(c for c in self.calls if c[0] == name)


def _after(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def test_the_whole_workflow_runs_in_order_and_briefs_the_digest_it_just_made(monkeypatch, tmp_path):
    world = World(monkeypatch, tmp_path)
    (world.digests / ".runs" / "zzz-old-run").mkdir(parents=True)
    (world.digests / ".runs" / "zzz-old-run" / "run.json").write_text("{}", encoding="utf-8")

    assert make_brief.main(["--24h"]) == 0

    assert world.steps == ["warm-up", "fetch", "digest", "run_system", "assemble"]
    run_argv = world.call("run_system")[1]
    assert _after(run_argv, "--export") == str(world.export)  # this digest's export, not merely the newest one
    assert _after(run_argv, "--model") == "qwen/qwen3.5-9b"
    assert _after(world.call("assemble")[1], "--run") == "run-1"  # the run just made, not the older one


@pytest.mark.parametrize("env, flag, expected", [
    (None, None, None),  # no key anywhere: none is passed on
    ("from-env", None, "from-env"),  # a key typed on the command line stays in shell history: the environment will do
    ("from-env", "from-flag", "from-flag"),  # but the flag wins when it is given
])
def test_the_llm_key_comes_from_the_flag_else_the_environment_else_nowhere(monkeypatch, tmp_path, env, flag, expected):
    if env is None:
        monkeypatch.delenv("XMD_LLM_API_KEY", raising=False)
    else:
        monkeypatch.setenv("XMD_LLM_API_KEY", env)
    world = World(monkeypatch, tmp_path)

    make_brief.main(["--24h", *(["--api-key", flag] if flag else [])])

    run_argv = world.call("run_system")[1]
    assert (_after(run_argv, "--api-key") if "--api-key" in run_argv else None) == expected


def test_it_says_what_to_open_waits_and_carries_on_by_itself_once_the_server_is_up(monkeypatch, tmp_path, capsys):
    world = World(monkeypatch, tmp_path)
    world.down_polls = 3

    assert make_brief.main(["--24h"]) == 0

    out = capsys.readouterr().out
    assert "Open LM Studio and start its server" in out and "carry on by myself" in out
    assert world.steps[0] == "warm-up" and world.steps[-1] == "assemble"  # nothing before the server answered


def test_it_says_nothing_about_LM_Studio_when_the_server_is_already_up(monkeypatch, tmp_path, capsys):
    World(monkeypatch, tmp_path)

    make_brief.main(["--24h"])

    assert "Open LM Studio" not in capsys.readouterr().out


def test_it_gives_up_without_fetching_when_the_server_never_comes_up(monkeypatch, tmp_path, capsys):
    world = World(monkeypatch, tmp_path)
    world.down_polls = 10**9

    assert make_brief.main(["--24h", "--wait", "0.05"]) == 1
    assert world.calls == []  # no fetch, no digest: the window is not used up
    assert "Open LM Studio" in capsys.readouterr().out

    assert make_brief.main(["--24h", "--wait", "0"]) == 1  # 0: no waiting at all
    assert world.calls == []


VENICE = "https://api.venice.ai/api/v1"
NO_THINKING = {"venice_parameters": {"disable_thinking": True}}


def test_a_server_elsewhere_that_is_down_is_not_waited_for_and_nobody_is_told_to_open_LM_Studio(monkeypatch, tmp_path, capsys):
    world = World(monkeypatch, tmp_path)
    world.down_polls = 5

    assert make_brief.main(["--24h", "--base-url", VENICE]) == 1  # the default --wait is 900 s

    assert world.calls == [] and world.down_polls == 4  # one look, no fetch
    out = capsys.readouterr().out
    assert "cannot reach the server" in out and "LM Studio" not in out


def test_the_environment_names_the_server_the_model_and_the_extra_body_and_a_flag_wins(monkeypatch, tmp_path):
    world = World(monkeypatch, tmp_path)
    monkeypatch.setenv("XMD_LLM_BASE_URL", VENICE)
    monkeypatch.setenv("XMD_LLM_MODEL", "other/model")
    monkeypatch.setenv("XMD_LLM_EXTRA_BODY", json.dumps(NO_THINKING))

    assert make_brief.main(["--24h"]) == 0

    run_argv = world.call("run_system")[1]
    assert _after(run_argv, "--base-url") == VENICE and _after(run_argv, "--model") == "other/model"
    assert json.loads(_after(run_argv, "--extra-body")) == NO_THINKING  # run_system records it for the assembly
    assert world.warmup_extra_body == NO_THINKING  # a model that thinks would fail the warm-up without it

    make_brief.main(["--24h", "--model", "qwen/qwen3.5-9b", "--extra-body", '{"reasoning_effort": "none"}'])
    run_argv = [c for c in world.calls if c[0] == "run_system"][-1][1]
    assert _after(run_argv, "--model") == "qwen/qwen3.5-9b"
    assert json.loads(_after(run_argv, "--extra-body")) == {"reasoning_effort": "none"}


def test_without_an_extra_body_none_is_passed_on(monkeypatch, tmp_path):
    world = World(monkeypatch, tmp_path)

    make_brief.main(["--24h"])

    assert "--extra-body" not in world.call("run_system")[1] and not world.warmup_extra_body


@pytest.mark.parametrize("text", ["{not json", '["a", "list"]'])
def test_an_extra_body_that_is_not_a_json_object_stops_before_anything_starts(monkeypatch, tmp_path, capsys, text):
    world = World(monkeypatch, tmp_path)

    with pytest.raises(SystemExit) as stop:
        make_brief.main(["--24h", "--extra-body", text])

    assert stop.value.code == 2 and world.calls == []
    assert "--extra-body" in capsys.readouterr().err


def test_a_model_name_that_fits_several_stops_before_anything_is_fetched(monkeypatch, tmp_path, capsys):
    world = World(monkeypatch, tmp_path)
    world.models = ["qwen/qwen3.5-9b", "qwen/qwen3.5-27b"]

    assert make_brief.main(["--24h", "--model", "qwen"]) == 2
    assert world.calls == []
    assert "fits 2 models" in capsys.readouterr().out


def test_a_model_the_server_does_not_offer_stops_before_anything_is_fetched(monkeypatch, tmp_path, capsys):
    world = World(monkeypatch, tmp_path)
    world.models = ["only/model"]

    assert make_brief.main(["--24h"]) == 2
    out = capsys.readouterr().out
    assert world.calls == []
    assert "qwen/qwen3.5-9b" in out and "only/model" in out  # what was wanted, and what is on offer


def test_a_model_that_does_not_answer_stops_before_anything_is_fetched(monkeypatch, tmp_path, capsys):
    world = World(monkeypatch, tmp_path)
    world.warmup_error = "spent its tokens thinking"

    assert make_brief.main(["--24h"]) == 1
    assert world.calls == []
    out = capsys.readouterr().out
    assert "spent its tokens thinking" in out and "Thinking must be off" in out


def test_nothing_new_in_the_window_makes_no_brief(monkeypatch, tmp_path):
    world = World(monkeypatch, tmp_path)
    world.export = None

    assert make_brief.main(["--24h"]) == 0
    assert world.steps == ["warm-up", "fetch", "digest"]


def test_no_fetch_skips_only_the_fetch(monkeypatch, tmp_path):
    world = World(monkeypatch, tmp_path)

    assert make_brief.main(["--24h", "--no-fetch"]) == 0
    assert world.steps == ["warm-up", "digest", "run_system", "assemble"]


def test_a_model_step_that_never_ran_is_not_assembled_and_the_retry_is_explained(monkeypatch, tmp_path, capsys):
    world = World(monkeypatch, tmp_path)
    world.writes_run, world.run_code = False, 1

    assert make_brief.main(["--24h"]) == 1
    assert "assemble" not in world.steps  # the newest run would be an old one
    out = capsys.readouterr().out
    assert "run_system.py --model qwen/qwen3.5-9b" in out and "assemble_brief.py" in out


def test_failed_chunks_still_get_a_brief_but_the_exit_code_says_so(monkeypatch, tmp_path):
    world = World(monkeypatch, tmp_path)
    world.run_code = 1

    assert make_brief.main(["--24h"]) == 1
    assert world.steps[-1] == "assemble"


def test_the_digests_and_the_reading_time_are_passed_on(monkeypatch, tmp_path):
    world = World(monkeypatch, tmp_path)

    make_brief.main(["--24h", "--full", "--quick", "--time", "10min"])

    assert world.call("digest") == ("digest", "24h", True, True)
    assert _after(world.call("run_system")[1], "--time") == "10"


def test_each_window_flag_picks_its_window_for_the_digest(monkeypatch, tmp_path):
    world = World(monkeypatch, tmp_path)

    make_brief.main(["--since-last-run"])
    make_brief.main(["--24h"])

    assert [c[1] for c in world.calls if c[0] == "digest"] == ["since-run", "24h"]


def test_the_window_must_be_chosen_and_only_one_of_them_and_nothing_starts_without_it(monkeypatch, tmp_path, capsys):
    world = World(monkeypatch, tmp_path)

    for argv in ([], ["--no-fetch"], ["--24h", "--since-last-run"]):
        with pytest.raises(SystemExit) as stop:
            make_brief.main(argv)
        assert stop.value.code == 2  # argparse's usage error

    assert world.calls == []  # not even the LM Studio check
    assert "one of the arguments --since-last-run --24h is required" in capsys.readouterr().err


def test_the_digest_step_says_which_window_it_covers(monkeypatch, tmp_path, capsys):
    World(monkeypatch, tmp_path)

    make_brief.main(["--since-last-run"])
    assert "only what is new since your last digest" in capsys.readouterr().out
    make_brief.main(["--24h"])
    assert "everything from the last 24 hours" in capsys.readouterr().out


def test_without_a_reading_time_the_size_is_left_to_run_system(monkeypatch, tmp_path):
    world = World(monkeypatch, tmp_path)

    make_brief.main(["--24h"])

    assert "--time" not in world.call("run_system")[1] and "--compression" not in world.call("run_system")[1]

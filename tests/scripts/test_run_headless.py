import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))  # run_headless imports its sibling scripts by name

import run_headless  # noqa: E402
from xmd.summary.engine import EngineError  # noqa: E402


def _label(args: tuple[str, ...]) -> str:
    """`lms server start`/`lms server stop` need both words to tell apart; every other subcommand is its first word."""
    if args[:2] == ("server", "start"):
        return "server_start"
    if args[:2] == ("server", "stop"):
        return "server_stop"
    return args[0]


class World:
    """Stands in for `lms`, the server it drives, and the pipeline scripts; records every `lms` call made of it."""

    def __init__(self, monkeypatch, server_up: bool = False):
        self.calls: list[tuple[str, tuple]] = []
        self.server_up = server_up  # whether the server answers before this run touches it
        self.fail: set[str] = set()  # labels (see _label) whose `lms` call should return nonzero
        self.ps_output = ""  # what `lms ps` prints, for the other-model-loaded warning
        self.brief_code = 0
        self.smoke_code = 0
        self.pipeline_argv: list[str] | None = None
        self.pipeline_raises: Exception | None = None
        monkeypatch.setattr(run_headless.shutil, "which", lambda name: "lms")
        monkeypatch.setattr(run_headless, "_call", self._call)
        monkeypatch.setattr(run_headless, "list_models", self._list_models)
        monkeypatch.setattr(run_headless.make_brief, "main", self._make_brief)
        monkeypatch.setattr(run_headless.smoke_engine, "main", self._smoke_engine)

    def _call(self, lms, *args, timeout=60):
        label = _label(args)
        self.calls.append((label, args))
        if label == "server_start":
            self.server_up = True
        elif label == "server_stop":
            self.server_up = False
        stdout = self.ps_output if label == "ps" else ""
        code = 1 if label in self.fail else 0
        return SimpleNamespace(returncode=code, stdout=stdout, stderr="lms failed" if code else "")

    def _list_models(self, base_url, api_key):
        if self.server_up:
            return ["qwen/qwen3.5-9b"]
        raise EngineError("connection refused")

    def _make_brief(self, argv):
        self.pipeline_argv = argv
        if self.pipeline_raises:
            raise self.pipeline_raises
        return self.brief_code

    def _smoke_engine(self, argv):
        self.pipeline_argv = argv
        if self.pipeline_raises:
            raise self.pipeline_raises
        return self.smoke_code

    def labels(self) -> list[str]:
        return [c[0] for c in self.calls]

    def args_for(self, label: str) -> tuple:
        return next(a for lbl, a in self.calls if lbl == label)


def test_it_starts_the_server_loads_the_model_runs_and_tears_down_in_order(monkeypatch):
    world = World(monkeypatch)

    assert run_headless.main(["--24h"]) == 0

    assert world.labels() == ["server_start", "ps", "load", "unload", "server_stop"]
    assert world.pipeline_argv == [
        "--24h", "--model", run_headless.make_brief.DEFAULT_MODEL, "--base-url", "http://127.0.0.1:1234/v1",
    ]


def test_load_gets_the_context_length_run_system_sizes_every_call_for(monkeypatch):
    world = World(monkeypatch)

    run_headless.main(["--24h"])

    load = world.args_for("load")
    assert load[0] == "load" and load[1] == run_headless.make_brief.DEFAULT_MODEL
    i = load.index("--context-length")
    assert load[i + 1] == str(run_headless.run_system.CONTEXT_TOKENS)
    assert "--gpu" not in load  # omitted unless the user asks: don't override the tuned preset


def test_gpu_is_forwarded_only_when_given(monkeypatch):
    world = World(monkeypatch)

    run_headless.main(["--24h", "--gpu", "max"])

    load = world.args_for("load")
    assert load[load.index("--gpu") + 1] == "max"


def test_an_already_up_server_is_reused_and_left_running(monkeypatch, capsys):
    world = World(monkeypatch, server_up=True)

    assert run_headless.main(["--24h"]) == 0

    assert "server_start" not in world.labels()
    assert "server_stop" not in world.labels()
    assert world.labels() == ["ps", "load", "unload"]  # only what this run brought up gets torn down
    assert "already up" in capsys.readouterr().out


def test_smoke_runs_smoke_engine_instead_of_make_brief(monkeypatch):
    world = World(monkeypatch)

    assert run_headless.main(["--smoke"]) == 0

    assert world.pipeline_argv is not None
    assert "--model" in world.pipeline_argv  # forwarded the same way either pipeline


def test_teardown_still_runs_when_the_pipeline_raises(monkeypatch):
    world = World(monkeypatch)
    world.pipeline_raises = RuntimeError("boom")

    with pytest.raises(RuntimeError):
        run_headless.main(["--24h"])

    assert world.labels()[-2:] == ["unload", "server_stop"]


def test_the_pipelines_exit_code_is_passed_through(monkeypatch):
    world = World(monkeypatch)
    world.brief_code = 1

    assert run_headless.main(["--24h"]) == 1


def test_a_load_failure_stops_before_the_pipeline_runs_and_tears_down(monkeypatch):
    world = World(monkeypatch)
    world.fail = {"load"}

    assert run_headless.main(["--24h"]) == 1

    assert world.pipeline_argv is None
    assert "server_stop" in world.labels()  # cleans up what it started, even without a model loaded


def test_lms_missing_exits_with_its_own_code_before_touching_anything(monkeypatch):
    World(monkeypatch)
    monkeypatch.setattr(run_headless.shutil, "which", lambda name: None)

    with pytest.raises(SystemExit) as stop:
        run_headless.main(["--24h"])

    assert stop.value.code == run_headless.LMS_MISSING


def test_a_different_model_already_loaded_only_warns(monkeypatch, capsys):
    world = World(monkeypatch)
    world.ps_output = "other/model   loaded\n"

    assert run_headless.main(["--24h"]) == 0

    assert "other/model" in capsys.readouterr().out
    assert world.pipeline_argv is not None  # a warning, not a block

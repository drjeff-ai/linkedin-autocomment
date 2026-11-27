"""Y2-S (D-033, DEC-076): YouTube comments on a schedule; approval stays per item.

``--scheduled`` refuses unless ``engagement.auto_run`` is true; ``youtube-morning``
finds and drafts up to today's cap minus the drafts waiting; an engage run that
posts nothing touches no Google call. Google is the fake from
``test_youtube_engage`` or :class:`NoGoogle`, which fails on any use.
"""

import json
import os
import subprocess
import sys
from datetime import timedelta

import pytest

from linkedin_automation import post_store
from linkedin_automation import youtube_api
from linkedin_automation import youtube_channel as yt
from linkedin_automation import youtube_engage as ye
import test_youtube_engage as base
from test_youtube_engage import (
    NOW, OLD_SCOPES, PROFILE, FakeApi, FakeGenerator, _today, engage, inserted,
    ledger_with, pt, quota_entries, seed, store, store_token_scopes, usage_lines,
    write_config)

#: The Y1 suite's fixture: a topfive profile in tmp, with a force-ssl token.
env = base.env

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OFF = "scheduled runs are off for topfive (engagement.auto_run is not true)"


class NoGoogle:
    """A ``youtube_api`` stand-in that fails the test on any use at all."""

    def __init__(self):
        self.touched = []

    def __getattr__(self, name):
        self.touched.append(name)
        raise AssertionError("youtube_api.%s was used" % name)


DROP = object()   # set_auto_run: remove the key


def set_auto_run(env, value):
    eng = env["cfg"]["scheduled_posting"]["platforms"]["youtube"]["engagement"]
    if value is DROP:
        eng.pop("auto_run", None)
    else:
        eng["auto_run"] = value
    write_config(env["cfg"])


def nothing_spent(env):
    return quota_entries() == [] and usage_lines(env) == []


# ── the switch ──────────────────────────────────────────────────────────────

def test_auto_run_defaults_to_false_and_reads_true(env):
    assert ye.engage_config(PROFILE).auto_run is False
    set_auto_run(env, True)
    assert ye.engage_config(PROFILE).auto_run is True
    set_auto_run(env, False)
    assert ye.engage_config(PROFILE).auto_run is False


@pytest.mark.parametrize("bad", ["true", 1, 0, None, [], {}])
def test_auto_run_of_another_type_is_refused_at_config_load(env, bad):
    set_auto_run(env, bad)
    with pytest.raises(ye.EngageRefused, match="auto_run must be true or false"):
        ye.engage_config(PROFILE)


@pytest.mark.parametrize("value", [DROP, False], ids=["absent", "false"])
def test_scheduled_engage_refuses_with_no_call_and_no_unit(env, value):
    set_auto_run(env, value)
    seed()
    api = NoGoogle()
    code, summary = ye.engage(PROFILE, now=NOW, api=api, out=env["out"], scheduled=True)
    assert code == ye.EXIT_REFUSED and summary["refused"] == OFF
    assert env["lines"] == ["youtube-engage topfive: REFUSED: " + OFF]
    assert api.touched == [] and nothing_spent(env)
    assert not os.path.exists(ye.ledger_path(PROFILE))
    assert post_store.is_approved(store().get("vidNEW1"))


def test_scheduled_engage_posts_when_auto_run_is_true(env):
    set_auto_run(env, True)
    seed()
    api = FakeApi(env)
    code, summary = ye.engage(PROFILE, now=NOW, api=api, out=env["out"], scheduled=True)
    assert code == ye.EXIT_OK and summary["posted"] == "vidNEW1"
    assert inserted(api) == ["commentThreads.insert"]


def test_without_the_flag_auto_run_false_changes_nothing(env):
    set_auto_run(env, False)
    seed()
    _c, summary, api = engage(env)
    assert summary["posted"] == "vidNEW1"


def test_scheduled_engage_still_posts_only_an_approved_draft(env):
    set_auto_run(env, True)
    seed(approve=False)
    api = NoGoogle()
    code, summary = ye.engage(PROFILE, now=NOW, api=api, out=env["out"], scheduled=True)
    assert code == ye.EXIT_OK and summary["posted"] is None
    assert summary["not_approved"] == 1 and api.touched == [] and nothing_spent(env)


# ── an engage run that posts nothing makes no call ─────────────────────────

def _no_approved(env):
    seed(approve=False)


def _outside_window(env):
    seed()
    return pt(23, 7, 59)


def _inside_gap(env):
    seed()
    ledger_with(next_allowed_at=yt.utc_z(NOW + timedelta(minutes=10)))


def _over_cap(env):
    seed()
    ledger_with(*_today(3), ramp_started_at=yt.utc_z(NOW - timedelta(hours=3)))


@pytest.mark.parametrize("setup", [_no_approved, _outside_window, _inside_gap, _over_cap],
                         ids=["no-approved", "outside-window", "inside-gap", "over-cap"])
def test_an_engage_that_posts_nothing_makes_no_google_call(env, setup):
    now = setup(env) or NOW
    api = NoGoogle()
    code, summary = ye.engage(PROFILE, now=now, api=api, out=env["out"])
    assert code == ye.EXIT_OK and summary["posted"] is None
    assert api.touched == [] and nothing_spent(env)


def test_the_no_google_guard_does_fail_a_run_that_would_post(env):
    """The other direction: the guard is not blind. An allowed, approved
    draft reaches Google, and NoGoogle stops the test."""
    seed()
    api = NoGoogle()
    with pytest.raises(AssertionError, match="youtube_api"):
        ye.engage(PROFILE, now=NOW, api=api, out=env["out"])
    assert api.touched


# ── youtube-morning ─────────────────────────────────────────────────────────

def morning(env, api=None, **kw):
    api = api or FakeApi(env)
    code, summary = ye.morning(PROFILE, now=NOW, api=api, out=env["out"],
                               make_generator=FakeGenerator, **kw)
    return code, summary, api


def statuses():
    return {k: r["status"] for k, r in store().posts.items()}


def test_morning_finds_then_drafts_up_to_the_cap_and_5_replies(env):
    code, summary, _api = morning(env)
    assert code == ye.EXIT_OK
    assert summary["limits"] == {ye.KIND_VIDEO: 3, ye.KIND_REPLY: 5}
    assert summary["find"]["videos"] == ["vidNEW1", "vidNEW2"]
    assert sorted(summary["draft"]["drafted"]) == ["cmtVIEWER", "vidNEW1", "vidNEW2"]
    assert ("youtube-morning topfive: ramp day 1, cap 3; 0 video draft(s) waiting -> "
            "draft up to 3; 0 reply draft(s) waiting of 5 -> draft up to 5") in env["lines"]


def test_morning_counts_waiting_drafts_approved_or_not(env):
    seed(key="vidW1")
    seed(key="vidW2", approve=False)
    seed(key="cmtW1", kind=ye.KIND_REPLY, approve=False)
    _c, summary, _api = morning(env)
    assert summary["counts"]["waiting_videos"] == 2
    assert summary["counts"]["waiting_replies"] == 1
    assert summary["limits"] == {ye.KIND_VIDEO: 1, ye.KIND_REPLY: 4}
    drafted = summary["draft"]["drafted"]
    assert len([k for k in drafted if k.startswith("vid")]) == 1
    assert statuses()["vidNEW2"] == post_store.NEW   # the newer one waits


def test_morning_floors_at_zero_when_more_are_waiting_than_the_cap(env):
    for i in range(4):
        seed(key="vidW%d" % i, approve=False)
    _c, summary, _api = morning(env)
    assert summary["limits"][ye.KIND_VIDEO] == 0
    assert summary["draft"]["drafted"] == ["cmtVIEWER"]
    assert statuses()["vidNEW1"] == post_store.NEW


def test_morning_uses_the_ramp_cap_of_the_day(env):
    ledger_with(ramp_started_at=yt.utc_z(NOW - timedelta(days=10)))
    _c, summary, _api = morning(env)
    assert summary["counts"]["ramp_day"] == 11 and summary["limits"][ye.KIND_VIDEO] == 5


def test_morning_never_approves_anything(env):
    seed(key="vidW1", approve=False)
    morning(env)
    for key, rec in store().posts.items():
        assert not post_store.is_approved(rec), key
        assert not rec.get("reviewed_at"), key


def test_morning_without_force_ssl_prints_both_refusals_and_exits_refused(env):
    store_token_scopes(OLD_SCOPES)
    code, summary, api = morning(env)
    assert code == ye.EXIT_REFUSED
    assert any("REFUSED the reply scan" in x for x in env["lines"])
    assert any(x.startswith("youtube-draft topfive: REFUSED") for x in env["lines"])
    assert summary["find"]["videos"] == ["vidNEW1", "vidNEW2"]
    assert not any(c.startswith("commentThreads") for c in api.calls)


def test_morning_drafts_after_a_find_error_and_exits_on_the_error(env):
    seed(key="vidOLDNEW", approve=False, status=post_store.NEW)
    api = FakeApi(env)

    def broken(*a, **k):
        raise youtube_api.YouTubeApiError("HTTP 500 backendError", ambiguous=True)
    api.channels_info = broken
    code, summary, _api = morning(env, api=api)
    assert code == ye.EXIT_ERROR
    assert summary["draft"]["drafted"] == ["vidOLDNEW"]


def test_morning_dry_run_calls_nothing_and_writes_nothing(env):
    api = NoGoogle()
    code, summary = ye.morning(PROFILE, dry_run=True, now=NOW, api=api, out=env["out"],
                               make_generator=lambda p, v: pytest.fail("generator built"))
    assert code == ye.EXIT_OK and api.touched == [] and nothing_spent(env)
    assert store().posts == {}
    assert any("would draft 0 of 0 NEW" in x for x in env["lines"])


def test_scheduled_morning_refuses_before_find(env):
    api = NoGoogle()
    code, summary = ye.morning(PROFILE, now=NOW, api=api, out=env["out"], scheduled=True)
    assert code == ye.EXIT_REFUSED and summary["refused"] == OFF
    assert env["lines"] == ["youtube-morning topfive: REFUSED: " + OFF]
    assert api.touched == [] and nothing_spent(env)


def test_scheduled_morning_runs_when_auto_run_is_true(env):
    set_auto_run(env, True)
    code, summary, _api = morning(env, scheduled=True)
    assert code == ye.EXIT_OK and len(summary["draft"]["drafted"]) == 3


def test_pick_new_limits_each_kind_and_skips_unnamed_kinds():
    recs = [{"key": "v%d" % i, "scraped_at": "2026-11-2%dT00:00:00Z" % i,
             "youtube": {"kind": ye.KIND_VIDEO}} for i in range(3)]
    recs += [{"key": "r1", "scraped_at": "2026-11-20T00:00:00Z",
              "youtube": {"kind": ye.KIND_REPLY}}]
    assert [r["key"] for r in ye._pick_new(recs, 0, {ye.KIND_VIDEO: 2})] == ["v0", "v1"]
    assert [r["key"] for r in ye._pick_new(recs, 0, {ye.KIND_VIDEO: 0,
                                                    ye.KIND_REPLY: 9})] == ["r1"]
    assert [r["key"] for r in ye._pick_new(recs, 2, None)] == ["v0", "r1"]


# ── the CLI ─────────────────────────────────────────────────────────────────

def cli():
    sys.path.insert(0, os.path.join(REPO, "tools"))
    import run_scheduled_posts
    return run_scheduled_posts


def test_the_cli_routes_morning_and_scheduled(env, monkeypatch):
    rsp = cli()
    seen = []
    monkeypatch.setattr(rsp.ye, "morning",
                        lambda p, **kw: seen.append(("morning", p, kw["dry_run"],
                                                     kw["scheduled"])) or (0, {}))
    monkeypatch.setattr(rsp.ye, "engage",
                        lambda p, **kw: seen.append(("engage", p, kw["dry_run"],
                                                     kw["scheduled"])) or (2, {}))
    for argv in (["youtube-morning", "--scheduled"], ["youtube-morning", "--dry-run"],
                 ["youtube-engage", "--scheduled"]):
        monkeypatch.setattr(sys, "argv", ["run_scheduled_posts.py"] + argv
                            + ["--profile", PROFILE])
        rsp.main()
    assert seen == [("morning", PROFILE, False, True), ("morning", PROFILE, True, False),
                    ("engage", PROFILE, False, True)]


@pytest.mark.parametrize("command", ["youtube-find", "youtube-draft", "youtube-watch"])
def test_the_cli_refuses_scheduled_on_other_commands(env, monkeypatch, command):
    rsp = cli()
    monkeypatch.setattr(sys, "argv", ["run_scheduled_posts.py", command,
                                      "--profile", PROFILE, "--scheduled"])
    with pytest.raises(SystemExit) as e:
        rsp.main()
    assert e.value.code == 2


def test_the_cli_returns_the_scheduled_refusal_exit_code(env, monkeypatch):
    rsp = cli()
    monkeypatch.setattr(sys, "argv", ["run_scheduled_posts.py", "youtube-engage",
                                      "--profile", PROFILE, "--scheduled"])
    monkeypatch.setattr(ye, "youtube_api", NoGoogle())
    assert rsp.main() == ye.EXIT_REFUSED


def test_printed_lines_reach_the_log_file(env, monkeypatch, tmp_path, capsys):
    rsp = cli()
    log = tmp_path / "logs" / "youtube-engage.log"

    def fake(p, out=print, **kw):
        out("youtube-engage topfive: 0 posted: a synthetic line")
        return 0, {}
    monkeypatch.setattr(rsp.ye, "engage", fake)
    monkeypatch.setattr(sys, "argv", ["run_scheduled_posts.py", "youtube-engage",
                                      "--profile", PROFILE, "--log-file", str(log)])
    try:
        assert rsp.main() == 0
    finally:
        import logging
        for h in list(logging.getLogger().handlers):
            if getattr(h, "baseFilename", None) == str(log):
                logging.getLogger().removeHandler(h)
                h.close()
        for h in logging.getLogger("run_scheduled_posts.out").handlers:
            h.close()
        logging.getLogger("run_scheduled_posts.out").handlers = []
    text = log.read_text(encoding="utf-8")
    assert text.count("0 posted: a synthetic line") == 1
    assert capsys.readouterr().out.count("0 posted: a synthetic line") == 1


def test_the_help_text_no_longer_says_nothing_is_on_a_schedule():
    rsp = cli()
    assert "Nothing here is on a schedule" not in rsp.__doc__
    assert "youtube-morning" in rsp.__doc__ and "--scheduled" in rsp.__doc__


# ── the task script (parsed, never run) ─────────────────────────────────────

_PS_PARSE = r"""
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $args[0], [ref]$tokens, [ref]$errors)
$cmds = $ast.FindAll({ param($n)
    $n -is [System.Management.Automation.Language.CommandAst] }, $true)
$assigns = @{}
$ast.FindAll({ param($n)
    $n -is [System.Management.Automation.Language.AssignmentStatementAst] }, $true) |
    ForEach-Object { $assigns[$_.Left.Extent.Text] = $_.Right.Extent.Text }
$out = @{
  errors = @($errors | ForEach-Object { $_.Message })
  commands = @($cmds | ForEach-Object { ,@($_.CommandElements | ForEach-Object { $_.Extent.Text }) })
  assigns = $assigns
}
$out | ConvertTo-Json -Depth 5 -Compress
"""


@pytest.fixture(scope="module")
def task_script(tmp_path_factory):
    if sys.platform != "win32":
        pytest.skip("PowerShell's parser is on Windows only")
    script = os.path.join(REPO, "tools", "setup_youtube_engage_tasks.ps1")
    probe = tmp_path_factory.mktemp("ps") / "parse.ps1"
    probe.write_text(_PS_PARSE, encoding="utf-8")
    out = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
         "Bypass", "-File", str(probe), script],
        capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    parsed = json.loads(out.stdout)
    assert parsed["errors"] == []
    return parsed


def _arg(cmd, flag):
    return cmd[cmd.index(flag) + 1]


def test_the_task_script_registers_the_two_commands(task_script):
    a = task_script["assigns"]
    assert a["$MorningTask"] == '"LinkedInCommentorYouTubeMorning-$Profile"'
    assert a["$EngageTask"] == '"LinkedInCommentorYouTubeEngage-$Profile"'
    assert a["$MorningArguments"] == (
        '"run --no-sync python tools\\run_scheduled_posts.py youtube-morning '
        '--profile $Profile --scheduled --log-file data\\logs\\youtube-engage.log"')
    assert a["$EngageArguments"] == (
        '"run --no-sync python tools\\run_scheduled_posts.py youtube-engage '
        '--profile $Profile --scheduled --log-file data\\logs\\youtube-engage.log"')
    cmds = task_script["commands"]
    actions = {_arg(c, "-Argument"): c for c in cmds
               if c[0] == "New-ScheduledTaskAction"}
    assert set(actions) == {"$MorningArguments", "$EngageArguments"}
    assert all(_arg(c, "-Execute") == "$Uv" for c in actions.values())
    assert any(c[:2] == ["Get-Command", "uv.exe"] for c in cmds)
    regs = {_arg(c, "-TaskName"): c for c in cmds if c[0] == "Register-ScheduledTask"}
    assert set(regs) == {"$MorningTask", "$EngageTask"}
    for name, pre in (("$MorningTask", "$Morning"), ("$EngageTask", "$Engage")):
        assert [_arg(regs[name], f) for f in ("-Action", "-Trigger", "-Settings",
                                              "-Principal")] == [
            pre + "Action", pre + "Trigger", pre + "Settings", "$Principal"]
    unregs = [_arg(c, "-TaskName") for c in cmds if c[0] == "Unregister-ScheduledTask"]
    assert sorted(unregs) == ["$EngageTask", "$MorningTask"]


def test_the_task_script_timing_and_limits(task_script):
    a = task_script["assigns"]
    assert a["$MorningTrigger"] == "New-ScheduledTaskTrigger -Daily -At '07:30'"
    assert a["$EngageTrigger"] == "New-ScheduledTaskTrigger -Daily -At '08:00'"
    assert a["$EngageTrigger.Repetition"] == (
        "(New-ScheduledTaskTrigger -Once -At '08:00' -RepetitionInterval "
        "(New-TimeSpan -Minutes 15) -RepetitionDuration (New-TimeSpan -Hours 13))"
        ".Repetition")
    for name, minutes in (("$MorningSettings", 20), ("$EngageSettings", 10)):
        s = a[name]
        assert "-StartWhenAvailable" in s and "-MultipleInstances IgnoreNew" in s
        assert "-ExecutionTimeLimit (New-TimeSpan -Minutes %d)" % minutes in s
    assert a["$Principal"] == ('New-ScheduledTaskPrincipal -UserId '
                               '"$env:USERDOMAIN\\$env:USERNAME" -LogonType '
                               'Interactive -RunLevel Limited')


def test_the_task_script_checks_the_profile_name_as_topup_does():
    src = open(os.path.join(REPO, "tools", "setup_youtube_engage_tasks.ps1"),
               encoding="utf-8").read()
    topup = open(os.path.join(REPO, "tools", "setup_topup_task.ps1"),
                 encoding="utf-8").read()
    assert "$Profile -notmatch '^[A-Za-z0-9_-]+$'" in src
    assert "'^[A-Za-z0-9_-]+$'" in topup

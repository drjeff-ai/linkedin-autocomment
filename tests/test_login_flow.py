"""Tests for login/session reliability: exit codes, login-required signalling,
the login_check utility, and session detection (ROADMAP Phase 3).

No Chrome or network: the browser boundary (create_driver / is_logged_in) is
stubbed."""

import time

from linkedin_automation import profile_manager as pm
from tools import login_check
from linkedin_automation import dashboard as linkedin_dashboard


# ─── Exit codes & error type ──────────────────────────────────────────────────

def test_exit_codes_are_distinct():
    assert pm.EXIT_OK == 0
    assert pm.EXIT_ERROR == 1
    assert pm.EXIT_LOGIN_REQUIRED == 2
    assert len({pm.EXIT_OK, pm.EXIT_ERROR, pm.EXIT_LOGIN_REQUIRED}) == 3


def test_login_required_error_is_runtimeerror():
    assert issubclass(pm.LoginRequiredError, RuntimeError)


# ─── login_check.status_report (pure) ─────────────────────────────────────────

def test_status_report_logged_in():
    msg, code = login_check.status_report(True, "demo")
    assert code == pm.EXIT_OK
    assert "logged in" in msg.lower()


def test_status_report_not_logged_in_is_actionable():
    msg, code = login_check.status_report(False, "demo")
    assert code == pm.EXIT_LOGIN_REQUIRED
    assert "login_check.py" in msg
    assert "demo" in msg


def test_status_report_defaults_profile_name():
    msg, _ = login_check.status_report(False, None)
    assert "default" in msg


# ─── session_exists ───────────────────────────────────────────────────────────

def test_session_exists_missing_dir(tmp_path):
    assert pm.session_exists(str(tmp_path / "nope")) is False


def test_session_exists_empty_dir(tmp_path):
    # Freshly created profile dir, no Chrome run yet.
    assert pm.session_exists(str(tmp_path)) is False


def test_session_exists_with_populated_default(tmp_path):
    default = tmp_path / "Default"
    default.mkdir()
    (default / "Cookies").write_text("x", encoding="utf-8")
    assert pm.session_exists(str(tmp_path)) is True


def test_session_exists_empty_string():
    assert pm.session_exists("") is False


# ─── run_job login-required mapping ───────────────────────────────────────────

def _wait_for_job(job_id, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = linkedin_dashboard.jobs.get(job_id)
        if job and job["status"] != "running":
            return job
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} did not finish in {timeout}s")


def test_run_job_login_required_is_clean(monkeypatch):
    def boom(jid):
        raise pm.LoginRequiredError(
            "Login required. Run: python login_check.py --profile demo"
        )

    linkedin_dashboard.run_job("test_login_clean", boom)
    job = _wait_for_job("test_login_clean")

    assert job["status"] == "failed"
    assert job.get("login_required") is True
    assert "login_check.py" in job["error"]
    # No raw traceback dumped for a login failure.
    assert not any("Traceback" in line for line in job["log"])


def test_run_job_generic_error_keeps_traceback():
    def boom(jid):
        raise ValueError("something else broke")

    linkedin_dashboard.run_job("test_generic_err", boom)
    job = _wait_for_job("test_generic_err")

    assert job["status"] == "failed"
    assert job.get("login_required") is not True
    assert any("Traceback" in line or "ERROR" in line for line in job["log"])


# ─── login_check.check_login with stubbed browser ─────────────────────────────

class _FakeDriver:
    def __init__(self):
        self.quit_called = False

    def get(self, url):  # login_check navigates to /feed/ itself now
        pass

    def quit(self):
        self.quit_called = True


def test_check_login_logged_in(monkeypatch, capsys):
    driver = _FakeDriver()
    monkeypatch.setattr(login_check.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(pm, "auto_migrate_from_env", lambda: None)
    monkeypatch.setattr(pm, "create_driver", lambda profile=None: (driver, {}))
    monkeypatch.setattr(pm, "is_logged_in_on_page", lambda d: True)

    code = login_check.check_login("demo", wait_for_manual=False)
    assert code == pm.EXIT_OK
    assert driver.quit_called is True


def test_check_login_not_logged_in_no_wait(monkeypatch):
    driver = _FakeDriver()
    monkeypatch.setattr(login_check.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(pm, "auto_migrate_from_env", lambda: None)
    monkeypatch.setattr(pm, "create_driver", lambda profile=None: (driver, {}))
    monkeypatch.setattr(pm, "is_logged_in_on_page", lambda d: False)

    code = login_check.check_login("demo", wait_for_manual=False)
    assert code == pm.EXIT_LOGIN_REQUIRED
    assert driver.quit_called is True


def test_check_login_unknown_profile(monkeypatch):
    def raise_value(profile=None):
        raise ValueError("Profile 'ghost' not found")

    monkeypatch.setattr(pm, "auto_migrate_from_env", lambda: None)
    monkeypatch.setattr(pm, "create_driver", raise_value)

    code = login_check.check_login("ghost", wait_for_manual=False)
    assert code == pm.EXIT_ERROR


def test_main_no_wait_passes_through(monkeypatch):
    captured = {}

    def fake_check(profile_name=None, wait_for_manual=True):
        captured["profile"] = profile_name
        captured["wait"] = wait_for_manual
        return pm.EXIT_OK

    monkeypatch.setattr(login_check, "check_login", fake_check)
    code = login_check.main(["--profile", "demo", "--no-wait"])
    assert code == pm.EXIT_OK
    assert captured == {"profile": "demo", "wait": False}

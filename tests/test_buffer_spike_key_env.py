"""tools/buffer_spike.py --key-env names the variable sent to Buffer as the
Bearer token, so it follows the dashboard's rule: BUFFER_API_KEY or
BUFFER_API_KEY_<SUFFIX>, nothing else.

Asserted on the requests.post the spike would make, not on its exit code.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools.buffer_spike as spike  # noqa: E402
from linkedin_automation import profile_manager as pm  # noqa: E402


LEAK = "value-that-must-never-leave-this-machine"
X_KEY = "x-buffer-key"
REFUSED = ["PATH", "OPENAI_API_KEY", "MY_ARBITRARY_TOKEN", "path", "X_BUFFER_API_KEY"]


class _Unauthorized:
    status_code = 401
    text = ""

    @staticmethod
    def json():
        return {}


@pytest.fixture
def sent(monkeypatch, tmp_path):
    """Every Authorization header the spike hands to requests.post. Buffer
    answers 401, so the run stops after one request. The transcript and usage
    log land in tmp_path, not the repo."""
    monkeypatch.chdir(tmp_path)
    headers = []

    class Transport:
        @staticmethod
        def post(url, **kwargs):
            headers.append(kwargs["headers"]["Authorization"])
            return _Unauthorized()

    monkeypatch.setattr(spike, "requests", Transport)
    monkeypatch.setattr(spike, "KEY_ENV_VAR", "BUFFER_API_KEY")
    for name in REFUSED:
        monkeypatch.setenv(name, LEAK)
    monkeypatch.setenv("BUFFER_API_KEY_X", X_KEY)
    return headers


def _run(monkeypatch, *argv):
    monkeypatch.setattr("sys.argv", ["buffer_spike.py", *argv])
    return spike.main()


@pytest.mark.parametrize("name", REFUSED)
def test_a_name_outside_the_rule_sends_nothing_and_states_the_rule(
        sent, monkeypatch, capsys, name):
    rc = _run(monkeypatch, "--key-env", name)
    out = capsys.readouterr().out
    assert sent == []
    assert rc == 2
    assert spike.KEY_ENV_RULE in out
    assert "Nothing was sent" in out
    assert LEAK not in out


def test_a_pasted_key_in_the_flag_is_not_printed(sent, monkeypatch, capsys):
    pasted = "a1b2c3d4e5f6g7h8i9j0buffersecretlookingvalue"
    rc = _run(monkeypatch, "--key-env", pasted)
    assert sent == []
    assert rc == 2
    assert pasted not in capsys.readouterr().out


def test_a_name_inside_the_rule_reaches_the_request(sent, monkeypatch):
    """The other direction, so the refusal above cannot pass by never asking."""
    assert _run(monkeypatch, "--key-env", "BUFFER_API_KEY_X") == 1   # the 401
    assert sent == ["Bearer %s" % X_KEY]


def test_the_default_still_reaches_the_request(sent, monkeypatch):
    monkeypatch.setenv("BUFFER_API_KEY", "bare-buffer-key")
    assert _run(monkeypatch) == 1
    assert sent == ["Bearer bare-buffer-key"]


@pytest.mark.parametrize("name", [
    "BUFFER_API_KEY", "BUFFER_API_KEY_X", "BUFFER_API_KEY_ALT_2",
    "PATH", "OPENAI_API_KEY", "path", "buffer_api_key_x", "XBUFFER_API_KEY",
    "BUFFER_API_KEYS", "BUFFER_API_KEY_", "BUFFER_API_KEY_x", "BUFFER_API_KEY-X",
    "BUFFER_API_KEY\nPATH", "BUFFER_API_KEY_" + "A" * 49, "BUFFER_API_KEY_" + "A" * 50,
    "",
])
def test_the_spike_copy_of_the_rule_agrees_with_profile_manager(name):
    """The spike copies the rule so it can run as a plain script. This keeps
    the copy from drifting."""
    assert spike.key_env_allowed(name) == bool(pm.scheduled_key_env(name))
    assert spike.KEY_ENV_RULE == pm.BUFFER_KEY_ENV_RULE

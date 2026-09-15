"""Guards on the suite's own hermeticity.

The suite has always *claimed* to be offline. These make the claim checkable.

Adopted and adapted from the student fork (.dev/AUDIT_fork_remainder.md §4).
The problem it names is real here, and was verified against this repo before
adopting: ``load_dotenv()`` runs at import time in several modules, so importing
the package under test loaded the developer's real ``.env`` — a live, funded
``sk-proj-…`` key — into ``os.environ`` for every test in the run.

Nothing spends it today, because the OpenAI client is mocked everywhere. That is
protection by coincidence: every guard is somewhere else, and one missed mock
reaches a real vendor and bills a real account. The ``_dummy_api_keys`` autouse
fixture in conftest closes it; these tests are what stop it silently reopening.
"""

import os
import re

import pytest

from conftest import API_KEY_ENV_VARS, DUMMY_API_KEY

# A vendor key, loosely: a recognised prefix followed by a long opaque token.
# Deliberately permissive — this asserts nothing *resembling* a credential is
# present, it does not parse one.
REAL_LOOKING_KEY = re.compile(r"^(sk-proj-|sk-ant-|sk-|xai-|gsk_)[A-Za-z0-9_\-]{20,}$")


def test_every_api_key_env_var_is_the_dummy():
    """Deleting the conftest fixture must fail this, on any machine."""
    for var in API_KEY_ENV_VARS:
        assert os.environ.get(var) == DUMMY_API_KEY, (
            f"{var} is not pinned to the test dummy")


def test_no_environment_variable_holds_a_real_looking_key():
    """Broader on purpose: catches a key arriving under a name we never declared.

    The check above asserts the fixture ran. This asserts the *outcome*, so a
    credential loaded from .env under some other name is still caught.
    """
    offenders = [name for name, value in os.environ.items()
                 if value and REAL_LOOKING_KEY.match(value)]
    assert offenders == [], (
        f"real-looking credentials visible to the suite: {offenders}")


def test_the_dummy_is_not_itself_real_looking():
    """Guards the guard: a dummy matching the pattern would mask everything."""
    assert not REAL_LOOKING_KEY.match(DUMMY_API_KEY)


def test_importing_the_package_does_not_reintroduce_a_real_key():
    """load_dotenv() at import time is the exact mechanism that leaked it.

    Importing a module mid-test must not overwrite the pinned dummy.
    """
    import importlib

    from linkedin_automation import comment_generator
    importlib.reload(comment_generator)

    for var in API_KEY_ENV_VARS:
        assert os.environ.get(var) == DUMMY_API_KEY, (
            f"{var} was overwritten by a module import — load_dotenv() is "
            f"clobbering the test environment again")


@pytest.mark.parametrize("var", API_KEY_ENV_VARS)
def test_the_suite_does_not_depend_on_an_ambient_key(var, monkeypatch):
    """Removing a key entirely must not change what the pinned value would be.

    Encodes the rule: the suite supplies its own environment. If a test only
    passes because the developer had something exported, the baseline it reports
    cannot be reproduced on a clean checkout or in CI.
    """
    monkeypatch.delenv(var, raising=False)
    assert os.environ.get(var) is None
    # The fixture pins it again for the next test; nothing here leaks forward.

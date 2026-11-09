"""Platform refactor P1: one source of platform ids, and a channel registry
that shared publishing code reads instead of branching on a name
(.dev/PLATFORM_REFACTOR.md §3, §4, §13).

The structural rule is enforced by parsing the code, not by grepping: no
comparison against a platform id anywhere in ``linkedin_automation/`` or
``tools/`` outside ``platforms.py`` and ``channels.py``, apart from three
named exemptions that each carry a reason, and fail if they go stale.
"""

import ast
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from linkedin_automation import channels  # noqa: E402
from linkedin_automation import csv_pipeline  # noqa: E402
from linkedin_automation import dashboard  # noqa: E402
from linkedin_automation import platform_policy  # noqa: E402
from linkedin_automation import platforms  # noqa: E402
from linkedin_automation import post_store  # noqa: E402
from linkedin_automation import profile_manager as pm  # noqa: E402

SCANNED_DIRS = ("linkedin_automation", "tools")

#: The registry itself: the only modules allowed to compare against an id.
REGISTRY_MODULES = {"linkedin_automation/platforms.py",
                    "linkedin_automation/channels.py"}

#: §13.5. Each is a module outside the publishing path that compares against a
#: platform id for its own reason. A stale entry fails
#: (test_every_exemption_is_still_needed): an exemption nothing needs is a hole
#: a future violation walks through unnoticed.
EXEMPT = {
    "linkedin_automation/post_store.py":
        "the engagement store: which platform's store reconciles from "
        "LinkedIn's derived files is the store's own policy, not a publishing "
        "channel's",
    "tools/x_dump.py":
        "X discovery (a non-goal of this refactor): reads the profiles.json "
        "test-account marker, not a channel",
    "tools/buffer_spike.py":
        "the Buffer spike: probes Buffer's own service names, including the "
        "old 'twitter', against the live schema; production never imports it",
}

#: Names that hold a platform id, a set of ids, or a channel declaration. A
#: comparison against one of these is a comparison against a platform.
#: ``channels.REGISTRY`` is deliberately absent: membership in the registry is
#: how a caller asks "is this a declared channel", which is the point of it.
ID_NAMES = {"LINKEDIN", "X", "DEFAULT", "ALL", "X_PLATFORM",
            "SCHEDULED_DEFAULT_PLATFORM", "PLATFORMS"}

#: String spellings of a platform. "twitter" is X's old name in Buffer.
ID_STRINGS = set(platforms.ALL) | {"twitter"}


def _is_id_string(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.strip().lower() in ID_STRINGS
    if isinstance(node, ast.JoinedStr):            # f"x"
        text = "".join(v.value for v in node.values
                       if isinstance(v, ast.Constant) and isinstance(v.value, str))
        return text.strip().lower() in ID_STRINGS
    return False


def _is_platform_operand(node):
    if _is_id_string(node):
        return True
    if isinstance(node, ast.Call):                 # "LinkedIn".casefold()
        return (isinstance(node.func, ast.Attribute)
                and _is_platform_operand(node.func.value))
    if isinstance(node, ast.Name):
        return node.id in ID_NAMES
    if isinstance(node, ast.Attribute):
        return node.attr in ID_NAMES
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return any(_is_platform_operand(e) for e in node.elts)
    return False


def _is_id_keyed_dict(node):
    return isinstance(node, ast.Dict) and any(
        k is not None and _is_platform_operand(k) for k in node.keys)


def platform_comparisons(source):
    """Line numbers of every branch on a platform id in ``source``:

    * ``==``/``!=``/``in``/``is`` with an id (or a set of ids) on either side;
    * ``case`` values;
    * ``.startswith``/``.endswith`` with an id argument;
    * dispatch through a dict literal keyed by ids (``{...}[p]``, ``{...}.get(p)``);
    * a module-level alias of an id, ``XNAME = platforms.X``, since a later
      ``p == XNAME`` would otherwise hide the branch.
    """
    tree = ast.parse(source)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            if any(_is_platform_operand(o) for o in [node.left] + node.comparators):
                found.append(node.lineno)
        elif isinstance(node, ast.MatchValue):
            if _is_platform_operand(node.value):
                found.append(node.value.lineno)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if (node.func.attr in ("startswith", "endswith")
                    and any(_is_platform_operand(a) for a in node.args)):
                found.append(node.lineno)
            elif node.func.attr == "get" and _is_id_keyed_dict(node.func.value):
                found.append(node.lineno)
        elif isinstance(node, ast.Subscript) and _is_id_keyed_dict(node.value):
            found.append(node.lineno)
    for node in tree.body:
        if (isinstance(node, ast.Assign) and isinstance(node.value, (ast.Name, ast.Attribute))
                and _is_platform_operand(node.value)
                and not all(isinstance(t, ast.Name) and t.id in ID_NAMES
                            for t in node.targets)):
            found.append(node.lineno)
    return sorted(found)


def _scanned_files():
    for top in SCANNED_DIRS:
        for dirpath, _dirs, files in os.walk(os.path.join(REPO_ROOT, top)):
            if "__pycache__" in dirpath:
                continue
            for name in files:
                if name.endswith(".py"):
                    full = os.path.join(dirpath, name)
                    yield os.path.relpath(full, REPO_ROOT).replace(os.sep, "/"), full


def _violations():
    by_file = {}
    for rel, full in _scanned_files():
        if rel in REGISTRY_MODULES:
            continue
        with open(full, encoding="utf-8") as f:
            lines = platform_comparisons(f.read())
        if lines:
            by_file[rel] = lines
    return by_file


# ─── The structural rule ────────────────────────────────────────────────────

def test_no_platform_comparison_outside_the_registry():
    offenders = {f: lines for f, lines in _violations().items() if f not in EXEMPT}
    assert offenders == {}, (
        "a branch on a platform id outside channels.py/platforms.py. If the "
        "behaviour differs by channel it is a declaration field "
        "(.dev/PLATFORM_REFACTOR.md §4): %s"
        % ", ".join("%s:%s" % (f, ",".join(map(str, ls)))
                    for f, ls in sorted(offenders.items())))


def test_every_exemption_is_still_needed():
    found = _violations()
    stale = sorted(f for f in EXEMPT if f not in found)
    assert stale == [], ("exempt but no longer compares against a platform id; "
                         "remove the exemption: %s" % stale)
    for f, reason in EXEMPT.items():
        assert os.path.isfile(os.path.join(REPO_ROOT, f)), f
        assert reason.strip(), f


@pytest.mark.parametrize("snippet", [
    'if platform == "x":\n    pass\n',
    'if platform != "linkedin":\n    pass\n',
    'ok = (platform or "").lower() == "X"\n',
    'if platform in ("linkedin", "pinterest"):\n    pass\n',
    'if service == "twitter":\n    pass\n',
    'if platform == platforms.X:\n    pass\n',
    'if p != pm.SCHEDULED_DEFAULT_PLATFORM:\n    pass\n',
    'if p == X_PLATFORM:\n    pass\n',
    'if channel is channels.LINKEDIN:\n    pass\n',
    'match platform:\n    case "x":\n        pass\n',
    'if platform.startswith("x"):\n    pass\n',
    'if platform.casefold() == "LinkedIn".casefold():\n    pass\n',
    'if platform == f"x":\n    pass\n',
    'handler = {"linkedin": a, "x": b}[platform]\n',
    'handler = {"linkedin": a, "x": b}.get(platform)\n',
    'XNAME = platforms.X\n',
    'if platform in platforms.ALL:\n    pass\n',
    'if platform in platform_policy.PLATFORMS:\n    pass\n',
])
def test_the_scanner_finds_each_shape_of_platform_branch(snippet):
    assert platform_comparisons(snippet) != []


@pytest.mark.parametrize("snippet", [
    'if channel.link_placement == channels.BODY:\n    pass\n',
    'if channel.identity_check == channels.BUFFER_HANDLE:\n    pass\n',
    'x = rect["x"] + 1\n',
    'store = PostStore(profile, platform=platforms.X)\n',
    'if platform not in channels.REGISTRY:\n    pass\n',
])
def test_the_scanner_ignores_reading_a_declaration(snippet):
    assert platform_comparisons(snippet) == []


def test_an_inserted_violation_in_a_real_module_is_caught():
    """The real file, plus one branch on a name: the scanner must see it."""
    path = os.path.join(REPO_ROOT, "linkedin_automation", "csv_pipeline.py")
    with open(path, encoding="utf-8") as f:
        source = f.read()
    assert platform_comparisons(source) == []
    mutated = source + '\n\ndef _bad(platform):\n    return platform == "x"\n'
    assert len(platform_comparisons(mutated)) == 1


# ─── One source of ids ──────────────────────────────────────────────────────

def test_every_copy_of_an_id_is_the_one_in_platforms():
    assert platform_policy.LINKEDIN is platforms.LINKEDIN
    assert platform_policy.X is platforms.X
    assert post_store.LINKEDIN is platforms.LINKEDIN
    assert post_store.X is platforms.X
    assert csv_pipeline.X_PLATFORM is platforms.X
    assert pm.SCHEDULED_DEFAULT_PLATFORM is platforms.DEFAULT


def test_no_module_but_platforms_defines_a_platform_string():
    """``NAME = "linkedin"`` anywhere else is a second spelling."""
    offenders = []
    for rel, full in _scanned_files():
        if rel == "linkedin_automation/platforms.py":
            continue
        with open(full, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in tree.body:
            if (isinstance(node, (ast.Assign, ast.AnnAssign))
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                    and node.value.value.lower() in ID_STRINGS):
                offenders.append("%s:%d" % (rel, node.lineno))
    assert offenders == []


# ─── The registry ───────────────────────────────────────────────────────────

def test_the_registry_declares_exactly_linkedin_and_x():
    assert set(channels.REGISTRY) == set(platforms.ALL) == {"linkedin", "x"}
    assert "pinterest" not in channels.REGISTRY      # P5, not P1
    for cid, channel in channels.REGISTRY.items():
        assert channel.id == cid


def test_the_declarations_match_the_amended_design():
    """§4 and §13.1-13.2, field by field."""
    li, x = channels.get("linkedin"), channels.get("x")
    assert (li.label, x.label) == ("LinkedIn", "X")
    assert (li.link_placement, x.link_placement) == (channels.FIRST_COMMENT,
                                                     channels.BODY)
    assert li.text_limit is None
    assert (x.text_limit.unit, x.text_limit.maximum, x.text_limit.url_cost) \
        == ("chars", 280, 23)
    assert (li.identity_check, x.identity_check) == (channels.UNGATED,
                                                     channels.BUFFER_HANDLE)
    assert (li.config_layout, x.config_layout) == (channels.FLAT, channels.OVERLAY)
    assert (li.key_env, x.key_env) == ("BUFFER_API_KEY", None)
    assert (li.key_env_editable, x.key_env_editable) == (False, True)
    assert (li.state_path, x.state_path) == (channels.LEGACY, channels.PLATFORM_DIR)
    assert (li.reconcile_statuses, x.reconcile_statuses) == (("sent",),
                                                             ("sent", "error"))


def test_xs_limit_is_the_generators_postable_limit_not_a_second_copy():
    policy = platform_policy.policy_for(platforms.X).length
    assert policy.hard_max_is_postable_limit
    assert channels.X.text_limit.maximum == policy.hard_max


def test_every_declared_field_is_read_outside_the_registry():
    """§11: a declaration nothing reads is a defect. Each field of a Channel and
    of a TextLimit must be read (``something.<field>``) by at least one module
    outside channels.py that imports channels, so an unrelated attribute of
    the same name elsewhere cannot satisfy it."""
    fields = {f for f in channels.Channel.__dataclass_fields__ if f != "id"}
    fields |= {"key_env_editable"}
    fields |= {f for f in channels.TextLimit.__dataclass_fields__ if f != "unit"}
    fields |= {"unit_word"}
    read = set()
    for rel, full in _scanned_files():
        if rel in REGISTRY_MODULES:
            continue
        with open(full, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        imports_channels = any(
            isinstance(n, ast.ImportFrom) and any(a.name == "channels" for a in n.names)
            for n in ast.walk(tree))
        if not imports_channels:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                read.add(node.attr)
    assert sorted(fields - read) == []


def test_a_declaration_that_breaks_the_field_coupling_is_refused():
    with pytest.raises(ValueError):
        channels.Channel(
            id="t", label="T", link_placement=channels.BODY, text_limit=None,
            identity_check=channels.BUFFER_HANDLE, config_layout=channels.FLAT,
            key_env=None, state_path=channels.PLATFORM_DIR,
            reconcile_statuses=("sent",))
    with pytest.raises(ValueError):
        channels.Channel(
            id="t", label="T", link_placement=channels.BODY, text_limit=None,
            identity_check=channels.BUFFER_HANDLE, config_layout=channels.OVERLAY,
            key_env="BUFFER_API_KEY_T", state_path=channels.PLATFORM_DIR,
            reconcile_statuses=("sent",))


def test_get_refuses_an_undeclared_name():
    with pytest.raises(channels.UnknownChannel):
        channels.get("pinterest")


def test_an_undeclared_name_resolves_to_the_old_else_branch():
    """Every P1 site was ``if linkedin: A else: B``. An undeclared name got B,
    and still does, so routing through the registry changed nothing for it."""
    c = channels.resolve("pinterest")
    assert c.link_placement == channels.NO_LINK
    assert c.text_limit is None
    assert c.identity_check == channels.BUFFER_HANDLE
    assert c.config_layout == channels.OVERLAY
    assert c.key_env_editable
    assert c.state_path == channels.PLATFORM_DIR
    assert c.reconcile_statuses == ("sent", "error")
    for odd in (None, "", "LinkedIn", " x"):
        assert channels.resolve(odd).config_layout == channels.OVERLAY


def test_the_flat_config_ids_are_linkedin_only():
    assert channels.flat_config_ids() == ["linkedin"]


# ─── The CLI's comment pass reads link_placement ────────────────────────────

def _run_cli(monkeypatch, capsys, platform):
    sys.path.insert(0, os.path.join(REPO_ROOT, "tools"))
    import run_scheduled_posts as cli

    calls = []
    monkeypatch.setattr(cli.pm, "resolve_scheduled", lambda profile, plat: {
        "channel_id": "chan", "identity_slug": "someone", "api_key": None})
    monkeypatch.setattr(cli.cp, "comment_pass",
                        lambda state, **kw: calls.append(kw) or [])
    monkeypatch.setattr(sys, "argv", ["run_scheduled_posts.py", "comment",
                                      "--profile", "p", "--platform", platform])
    code = cli.main()
    return code, calls, capsys.readouterr().out


def test_the_cli_refuses_a_comment_pass_for_a_body_link_channel(monkeypatch, capsys):
    code, calls, out = _run_cli(monkeypatch, capsys, "x")
    assert code == 2
    assert calls == []
    assert "the comment pass is LinkedIn-only" in out


def test_the_cli_runs_the_comment_pass_for_a_first_comment_channel(monkeypatch, capsys):
    code, calls, out = _run_cli(monkeypatch, capsys, "linkedin")
    assert code == 0
    assert [c["platform"] for c in calls] == ["linkedin"]


# ─── _req_platform validates against the channel registry ───────────────────

@pytest.fixture
def client():
    dashboard.app.config["TESTING"] = True
    return dashboard.app.test_client()


def test_req_platform_accepts_a_channel_the_generator_has_never_heard_of(
        client, monkeypatch):
    extra = channels.undeclared("testchannel")
    monkeypatch.setitem(channels.REGISTRY, "testchannel", extra)
    assert "testchannel" not in platform_policy.PLATFORMS
    res = client.get("/api/scheduled/p/settings?platform=testchannel")
    assert res.status_code == 200, res.get_json()
    assert res.get_json()["platform"] == "testchannel"


def test_req_platform_refuses_a_voice_that_is_not_a_channel(client, monkeypatch):
    monkeypatch.setitem(platform_policy.PLATFORMS, "voiceonly",
                        platform_policy.PLATFORMS["x"])
    res = client.get("/api/scheduled/p/settings?platform=voiceonly")
    assert res.status_code == 400
    assert "unknown platform 'voiceonly' - known: linkedin, x" in res.get_json()["error"]

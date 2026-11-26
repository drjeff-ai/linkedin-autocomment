"""V1 (D-026) and I1 (D-030): adding YouTube and Instagram left every existing
channel byte-identical.

``GOLDEN`` is the field-by-field value of the Buffer registry captured on main
at 230f1fc, before V1 changed ``channels.py``. The only additions to an existing
channel are fields whose defaults change nothing: V1's ``publisher`` (Buffer)
and I1's ``video_column``, ``video_max_bytes``, ``media_required``,
``max_hashtags`` and ``preflight``. Instagram is appended after them.
"""

import dataclasses
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from linkedin_automation import channels  # noqa: E402
from linkedin_automation.channels import ExtraColumn, TextLimit  # noqa: E402

GOLDEN = [
    ("linkedin", {"id": "linkedin", "label": "LinkedIn", "link_placement": "first_comment",
                  "text_limit": None, "identity_check": "ungated", "config_layout": "flat",
                  "key_env": "BUFFER_API_KEY", "state_path": "legacy",
                  "reconcile_statuses": ("sent",), "extra_columns": (),
                  "metadata_builder": None, "alt_text_column": None,
                  "image_required": False, "board_lookup": False}),
    ("x", {"id": "x", "label": "X", "link_placement": "body",
           "text_limit": TextLimit(unit="chars", maximum=280, url_cost=23),
           "identity_check": "buffer_handle", "config_layout": "overlay", "key_env": None,
           "state_path": "platform_dir", "reconcile_statuses": ("sent", "error"),
           "extra_columns": (), "metadata_builder": None, "alt_text_column": None,
           "image_required": False, "board_lookup": False}),
    ("pinterest", {"id": "pinterest", "label": "Pinterest", "link_placement": "metadata",
                   "text_limit": TextLimit(unit="chars", maximum=500, url_cost=None),
                   "identity_check": "buffer_handle", "config_layout": "overlay",
                   "key_env": None, "state_path": "platform_dir",
                   "reconcile_statuses": ("sent", "error"),
                   "extra_columns": (ExtraColumn(name="title", required=True, limit=100),
                                     ExtraColumn(name="alt_text", required=False, limit=500),
                                     ExtraColumn(name="board", required=True, limit=None)),
                   "metadata_builder": "linkedin_automation.channels._pinterest_metadata",
                   "alt_text_column": "alt_text", "image_required": True,
                   "board_lookup": True}),
]


def _fields(c):
    out = {}
    for f in dataclasses.fields(c):
        v = getattr(c, f.name)
        if callable(v):
            v = "%s.%s" % (v.__module__, v.__qualname__)
        out[f.name] = v
    return out


#: I1's fields, at the value every channel before Instagram must hold.
I1_DEFAULTS = {"video_column": None, "video_max_bytes": None,
               "media_required": False, "max_hashtags": None, "preflight": None}


def test_every_existing_channel_is_unchanged():
    now = [(cid, _fields(c)) for cid, c in channels.REGISTRY.items()]
    assert [cid for cid, _ in now] == [cid for cid, _ in GOLDEN] + ["instagram"]
    for (cid, fields), (_gid, golden) in zip(now, GOLDEN):
        assert fields.pop("publisher") == channels.BUFFER, cid
        assert {k: fields.pop(k) for k in I1_DEFAULTS} == I1_DEFAULTS, cid
        assert fields == golden, cid


def test_the_golden_goes_red_when_an_existing_channel_moves():
    """The comparison above can fail: a Pinterest with any I1 field set, or
    any old field changed, no longer equals its golden."""
    pin = dict(GOLDEN)["pinterest"]
    for change in ({"max_hashtags": 30}, {"image_required": False}):
        moved = _fields(dataclasses.replace(channels.PINTEREST, **change))
        moved.pop("publisher")
        popped = {k: moved.pop(k) for k in I1_DEFAULTS}
        assert popped != I1_DEFAULTS or moved != pin, change


def test_instagram_is_declared_as_i1_specified():
    fields = _fields(channels.REGISTRY["instagram"])
    assert fields == {
        "id": "instagram", "label": "Instagram", "link_placement": "refused",
        "text_limit": TextLimit(unit="chars", maximum=2200, url_cost=None),
        "identity_check": "buffer_handle", "config_layout": "overlay",
        "key_env": None, "state_path": "platform_dir",
        "reconcile_statuses": ("sent", "error"),
        "extra_columns": (ExtraColumn(name="video_path", required=False, limit=None),),
        "metadata_builder": "linkedin_automation.channels._instagram_metadata",
        "alt_text_column": None, "image_required": False, "board_lookup": False,
        "publisher": "buffer", "video_column": "video_path",
        "video_max_bytes": 300000000, "media_required": True, "max_hashtags": 30,
        "preflight": "linkedin_automation.channels._instagram_preflight"}


def test_resolve_and_get_answer_as_before_for_every_existing_name():
    for cid, _ in GOLDEN:
        assert channels.resolve(cid) is channels.REGISTRY[cid] is channels.get(cid)
    # A Buffer site asking about youtube still gets the old else-branch.
    assert channels.resolve("youtube") == channels.undeclared("youtube")


def test_the_golden_test_fails_on_a_changed_field(monkeypatch):
    """The comparison is not vacuous: one changed field makes it fail."""
    changed = dataclasses.replace(channels.X, reconcile_statuses=("sent",))
    monkeypatch.setitem(channels.REGISTRY, "x", changed)
    try:
        test_every_existing_channel_is_unchanged()
    except AssertionError:
        return
    raise AssertionError("the golden test passed with a changed X declaration")

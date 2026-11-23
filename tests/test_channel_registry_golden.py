"""V1 (D-026): adding YouTube left every existing channel byte-identical.

``GOLDEN`` is the field-by-field value of the Buffer registry captured on main
at 230f1fc, before V1 changed ``channels.py``. The only addition to an existing
channel is the new ``publisher`` field, whose default is Buffer.
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


def test_every_existing_channel_is_unchanged():
    now = [(cid, _fields(c)) for cid, c in channels.REGISTRY.items()]
    assert [cid for cid, _ in now] == [cid for cid, _ in GOLDEN]
    for (cid, fields), (_gid, golden) in zip(now, GOLDEN):
        assert fields.pop("publisher") == channels.BUFFER, cid
        assert fields == golden, cid


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

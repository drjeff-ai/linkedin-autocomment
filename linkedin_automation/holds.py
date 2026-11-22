"""App-side holds for a channel's source pins (F7).

A hold is a source pin the app has mapped but will not queue yet, with the
reason. Held pins are **not** in the pipeline state file: the state file only
ever holds rows that are queued or further on, so ``topup``,
``pending_rows`` and every other reader of it are unchanged by holds. This
module is the only writer of the hold file.

One file per profile and channel, beside the channel's state file:
``<state dir>/pin_holds.json`` =
``{"holds": {source_id: {source_id, book, series, kind, reason, held_at,
source, row}}}``.

* ``source``: the full source row as read from the book's ``pins.csv``.
* ``row``: the row it maps to (no date: a held row has no slot).
* ``kind``: ``board`` or ``link`` (re-checked by ``recheck-holds``),
  ``review`` or ``operator`` (released only by ``release-hold``).

Writes are temp-then-rename, never truncate-then-write.
"""

import json
import os
from datetime import datetime, timezone

HOLDS_NAME = "pin_holds.json"

KIND_BOARD = "board"
KIND_LINK = "link"
KIND_REVIEW = "review"
KIND_OPERATOR = "operator"
KINDS = (KIND_BOARD, KIND_LINK, KIND_REVIEW, KIND_OPERATOR)
#: Kinds whose reason is a check the app can re-run on its own.
RECHECKABLE = (KIND_BOARD, KIND_LINK)


class HoldError(ValueError):
    """A hold that cannot be written as asked."""


def holds_path(state_path):
    """The hold file that belongs to the channel whose state file is
    ``state_path``."""
    return os.path.join(os.path.dirname(os.path.abspath(state_path)),
                        HOLDS_NAME)


class HoldStore:
    """The hold file of one profile and channel."""

    def __init__(self, path):
        self.path = path
        self.holds = self._load()

    def _load(self):
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            # Reading a corrupt hold file as empty would let every held pin be
            # queued as new. Refuse instead.
            raise HoldError("hold file %s is unreadable; refusing to treat it "
                            "as empty" % self.path)
        holds = data.get("holds") if isinstance(data, dict) else None
        if not isinstance(holds, dict):
            raise HoldError("hold file %s has no 'holds' object" % self.path)
        return holds

    def reload(self):
        self.holds = self._load()

    def keys(self):
        return set(self.holds)

    def get(self, source_id):
        return self.holds.get(source_id)

    def of_kind(self, kinds=None):
        """Holds of ``kinds`` (all when None), ordered by when they were held
        and then by id, so a listing reads the same every time."""
        out = [h for h in self.holds.values()
               if kinds is None or h.get("kind") in kinds]
        out.sort(key=lambda h: (h.get("held_at") or "", h.get("source_id")))
        return out

    def put(self, source_id, *, book, series, kind, reason, source, row,
            now=None):
        """Hold ``source_id``, or update its reason and kind if it is held.

        The first ``held_at`` is kept on an update, so a pin that has been
        failing its link check for a week says so."""
        if kind not in KINDS:
            raise HoldError("unknown hold kind %r - expected one of %s"
                            % (kind, ", ".join(KINDS)))
        if not source_id or not isinstance(source_id, str):
            raise HoldError("a hold needs a source id")
        if not str(reason or "").strip():
            raise HoldError("a hold needs a reason")
        stamp = (now or datetime.now(timezone.utc)).isoformat()
        old = self.holds.get(source_id) or {}
        self.holds[source_id] = {
            "source_id": source_id, "book": book, "series": series,
            "kind": kind, "reason": str(reason).strip(),
            "held_at": old.get("held_at") or stamp,
            "checked_at": stamp,
            "source": dict(source or {}), "row": dict(row or {}),
        }
        return self.holds[source_id]

    def remove(self, source_id):
        return self.holds.pop(source_id, None)

    def save(self):
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"holds": self.holds}, f, indent=2, sort_keys=True)
        os.replace(tmp, self.path)

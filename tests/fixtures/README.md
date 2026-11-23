# tests/fixtures — saved DOM shapes for the offline selector gate

Every file here is **hand-authored and synthetic**. No real name, profile URL, or
post text from a live feed appears in any of them. They imitate the *shape* of
LinkedIn's DOM, which is the only part the selectors care about.

They exist so the selector watchdog has a gate that runs with no browser, no
network, and no LinkedIn session. Previously the only way to find out whether a
selector still matched was to run the tool against the live site — which meant
the check could only be run by hand, on a logged-in machine, after the breakage.

Driven by `tests/test_selector_fixture_gate.py` via
`linkedin_automation/dom_probe.py`.

## What these prove, and what they do not

| Check | Catches | Misses |
|---|---|---|
| **Fixture** (offline, here) | someone edited a selector constant and broke the match | LinkedIn changing its DOM |
| **Live** (`--post-url`, feed run) | LinkedIn changed its DOM | nothing — but needs a session, a browser, and a human |

A fixture is a frozen snapshot of a shape that was once correct. Passing against
it means *the code still matches what it was written for*; it says nothing about
what LinkedIn serves today. **Neither check substitutes for the other.**
See docs/ARCHITECTURE.md §8.3.

| File | Page | Expected result |
|---|---|---|
| `feed_healthy.html` | feed | `HEALTHY` — every non-gated feed entry matches |
| `feed_degraded.html` | feed | `DEGRADED` — a non-critical hook renamed |
| `composer_open.html` | composer | `HEALTHY` — the post composer open; the only state where `composer_editor` and `composer_post_button` exist |
| `feed_broken.html` | feed | `BROKEN` — the critical post container renamed |
| `feed_menu_open.html` | feed | `HEALTHY` — overflow menu expanded, so `copy_link_item` is actually checked |
| `post_healthy.html` | post | `HEALTHY` — permalink as loaded, comment box closed |
| `post_box_open.html` | post | `HEALTHY` — composer open, so the editor and submit button exist |

Each variant states, in an HTML comment at the top, exactly which hook was
renamed relative to `feed_healthy.html`. Keep that comment accurate: it is the
only thing that explains why a fixture is expected to fail.

The degraded/broken pair each change **exactly one attribute**. That is the shape
of every real break this project has seen: a hook is renamed, nothing raises, and
the code simply finds nothing.

`post_box_open.html` carries the trap that broke posting. It has **two** buttons
involving the word Comment: the action-bar button with `aria-label="Comment"`
whose visible text is a count, and the submit button with no aria-label whose
visible text is `Comment`. A check that cannot tell them apart reports success on
the wrong one — which is why `SUBMIT_BUTTON_XPATH` is XPath, matched on visible
text, and not CSS.

## Rules for adding one

1. **Hand-author it. Never paste a live DOM.** Real LinkedIn markup carries real
   names, profile URLs, headlines and activity URNs.
2. Person and company slugs must start with `example-` (`/in/example-person-one/`).
3. Activity URNs must be all zeros (`urn:li:activity:0000000000000000000`).
4. No email addresses.
5. Change **one** thing per variant, and say which in the header comment.

Rules 2–4 are enforced by
`test_selector_fixture_gate.py::test_no_fixture_contains_personally_identifying_data`,
which runs over every `*.html` here. A fixture captured by copy-paste from a live
session trips it rather than landing in the repo.

## X fixtures

Same rules, same reasoning, a different site. Driven by
`tests/test_x_selector_registry.py` against the `page="x_*"` entries in
`linkedin_automation/selector_health.py`, which pull their selectors from
`linkedin_automation/x_selectors.py`.

| File | Page | Expected result |
|---|---|---|
| `x_timeline_healthy.html` | x_timeline | `HEALTHY` — every timeline entry matches |
| `x_timeline_broken.html` | x_timeline | `BROKEN` — the critical card container renamed |
| `x_status_healthy.html` | x_status | `HEALTHY` — permalink page, composer present on load |
| `x_search_healthy.html` | x_search | `HEALTHY` — same cards as the timeline, plus search chrome |
| `x_timeline_mixed.html` | (finder only) | not a gate fixture — the awkward real shapes, see below |

These are **shaped from** the live captures, never copied from them. The four
scrubbed dumps live in `data/<profile>/x_spike/` (gitignored, machine-local) and
are ~240KB of X's real markup; what crosses into the repo is only the hooks the
selectors care about, re-authored by hand. Rule 1 above applies unchanged.

Three X-specific conventions, so the PII gate passes by construction:

1. Handles are `user1`, `user2`, … — the same synthetic form `x_dump`'s scrubber
   produces, so `UserAvatar-Container-user1` classifies as an obviously-fake
   identity rather than a real one.
2. Status ids are all 9s (`9999999999999990001`), like the scrubber's output.
3. Follow buttons are `<all-9s>-follow`, because X puts the account id *in* the
   testid and the gate checks the tail.

`test_no_fixture_contains_personally_identifying_data` enforces all three by
running every `data-testid` here through `tools.x_dump.classify_testid` — the
same classifier the dump scrubber uses, so a fixture pasted from a live X
session trips it rather than landing in the repo.

### Two things these fixtures deliberately encode

**The permalink is a plain anchor.** Each card carries `/…/status/…` as an
ordinary href, with the `/analytics` variant beside it. LinkedIn needs the
overflow menu opened and the clipboard read; X needs neither, so there is no
menu-gated X entry and no X equivalent of `copy_link_item`.

**The composer is on screen before anything is clicked.** The harvest dumped a
status page twice — as loaded, and after clicking the reply box — and both held
identical structural testids. So no X entry is gated, and `x_status_healthy.html`
shows the editor and submit button at page load. `tweetTextarea_0` also appears
in `x_timeline_healthy.html`, which is not a mistake: X mounts an inline composer
on the timeline too, and that overlap is exactly why the reply editor is filed
under `page="x_status"` and must be scoped rather than looked up document-wide.

### `x_timeline_mixed.html` — the shapes the captures actually held

The gate fixtures above are clean cases. This one is for the **finder**
(`tests/test_x_finder.py`) and is not checked by the selector gate. Every card
in it reproduces something found by parsing the four real scrubbed dumps:

| Card | Shape | Why it matters |
|---|---|---|
| A | permalink with `/analytics` **and** `/photo/1` siblings | either sibling, taken as the URL, writes a store key the poster cannot navigate back to |
| B | a quote tweet — two `User-Name`, two `<time>`, one `tweetText` | cards nest (3 of 5 cards in the timeline capture); every per-card lookup must take the FIRST match or it reads the quoted account as the author |
| C | a paid ad: external `utm_medium=paid_social_media` link, no `<time>`, no canonical permalink | exactly one card in the capture had this shape; it cannot be addressed, so the finder skips it |
| D | `/i/status/<id>` | `i` is one of X's own routes, not a handle — addressable, but with no account in the URL |
| E | a re-render of card A, same permalink | X repeats a card across rows, so counting rows double-counts |

Expected: three posts parsed (A, B, D). C is skipped, E collapses into A.

Counts are written in X's real phrasing, singular forms included (`1 Reply.
Reply`, `1 repost. Repost`), because that is what the captures held.

## `instagram/` (D-027 E3)

Synthetic Instagram pages (feed, post, liked post, post with our comment,
removed post, a broken post, profiles), written by hand from the
nine-month-stale instagram_bot reference and **unverified**: no live
Instagram capture exists yet. `tests/fake_ig_driver.py` serves them to the
adapter; `tests/test_instagram_adapter.py` runs the `ig_*` registry entries
against them. Every handle and caption is invented.

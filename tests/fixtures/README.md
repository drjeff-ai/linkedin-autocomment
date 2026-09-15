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
See ARCHITECTURE.md §8.3.

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

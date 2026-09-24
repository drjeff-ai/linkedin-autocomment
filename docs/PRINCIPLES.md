# PRINCIPLES

How work is done on this repo. Each rule here was paid for by a real outage;
the incident behind it is in `docs/MAINTENANCE.md` or `docs/ARCHITECTURE.md`.
These are not style preferences. A change that breaks one needs a stated reason.

---

## 1. Evidence over hypothesis

**Dump the DOM. Never guess.** LinkedIn ships hashed class names and rotates
markup without notice, so what a selector "should" match is worth nothing. Read
the live page (`tools/feed_dump.py`, a failure capture's `.html`, the
`*_submitdom.json`) and derive the fix from what is actually there.

**A fix that fails once earns observability, not another guess.** If the first
fix for a live failure does not work, the next change adds capture — what was
on the page, which element was chosen, what state it was in — not a second
theory. The comment outage took three dispatches because each one fixed the
leading hypothesis; the fix that held came from the capture that showed two
enabled buttons both reading "Comment" (MAINTENANCE §6.2).

## 2. Observability first — a silent failure is the bug

A path that can fail without saying so is broken even on the runs where it
works. Every failure the tool can hit on LinkedIn must leave a named reason and
evidence behind (`data/<profile>/failures/`), and every run summary must count
what it did *not* do. "0 posted" with no reason is a defect, not a result.

## 3. Verifiers must be provably able to return both True and False

A check whose passing condition is "we found the thing" needs a test that feeds
it a page where the thing **is** present and asserts True, plus the matching
page where it is absent and asserts False.

A verifier that can only say no looks like rigour and behaves like an outage: it
reports every real success as a failure, the queue never drains, and a fallback
submit posts twice (MAINTENANCE §6.5). A verifier that can only say yes is a
rubber stamp. Prove both directions or it is not a verifier.

## 4. Bound every wait — a dead selector costs seconds, not minutes

Selectors die. When one does, the cost must be small and fixed. Waits are
bounded in **total**, across all selectors and signals, and polled — never a
chain of per-selector `WebDriverWait`s whose cost grows with every fallback
added. The like lookup cost sixty seconds a comment and the dead-post check two
minutes a post before this rule; both are now a few seconds (MAINTENANCE §6.8,
§7). Do not sleep through a wait the code already performs.

## 5. Terminal vs transient — never terminal-mark on a weak signal

A terminal state (`COMMENTED`, `UNAVAILABLE`, manual `TRASH`) removes a record
from all future consideration, and nothing reviews it afterwards. So a record
goes terminal only on a **positive** signal: the comment seen in the thread,
the redirect off the activity id. Everything ambiguous — a slow load, an auth
wall, a page shape not seen before — is transient and retried.

The asymmetry is deliberate: a false transient costs one retry; a false terminal
silently loses a post, or, after an expired session, the whole queue
(MAINTENANCE §7.2–§7.3).

## 6. Fail loud, not silent

When the tool cannot do the safe thing, it stops and says so. It does not guess,
fall back to a broader match, or record success it did not observe. A
"page-wide fallback" that resolves to the wrong button is the same bug in a new
costume (MAINTENANCE §6.2). A non-zero exit, a named reason and a capture are
always better than a plausible-looking run.

## 7. Separate recon from repair

Investigation and change are different dispatches. Recon captures, reads and
reports — it does not edit. Repair changes code against evidence recon already
produced. Mixing them is how a diagnosis gets bent to fit the fix already
written.

## 8. Validate live before merge

Unit tests and offline fixtures prove the code matches the shape it was written
for; they prove nothing about what LinkedIn serves today (ARCHITECTURE §8.3). A
change to anything that touches LinkedIn is not done until it has run live on a
small batch and the result was checked on LinkedIn itself — not in the tool's
own report. Merge after that, not before.

## 9. Selector discipline

- **Stable hooks first:** `aria-label`, `role`, `data-testid`, visible text,
  structure. Hashed classes last, and only as fallbacks.
- **Scope past decoys.** When the page carries look-alikes — two "Comment"
  buttons, six "Open reactions menu" buttons beside the Like — match the exact
  label or scope structurally to the right container. Never loosen a selector
  to make it match; that is how decoys get clicked.
- **Dump and re-derive.** When a selector dies, capture the page and derive the
  replacement from it. New hook first, old ones kept as bounded fallbacks
  (MAINTENANCE §3 step 4).
- **Every selector is a class constant** on its owner class, so the selector
  health registry can watch it (ARCHITECTURE §8.1).
- **Mark what is inferred.** A selector derived from docs or from a related
  shape, not from a capture, says so in a comment until a capture confirms it.

## 10. Security

- **No PII in the repo.** Member names, profile slugs, post text and live page
  captures never enter version control. Test fixtures are synthetic.
- **`.env` and `data/` are gitignored** and stay that way; so are DOM dumps,
  `api_usage.jsonl`, `CLAUDE.md` and `.dev/`. Stage files explicitly.
- **Push only `main`.** Feature branches stay local.
- **`identity_slug` is set** for every profile that posts. An empty slug
  disables the identity guard, and acting as the wrong account cannot be undone
  (ARCHITECTURE §9).

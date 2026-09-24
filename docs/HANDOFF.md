# HANDOFF (executor pointer)

**The canonical current-state handoff is the project HANDOFF doc, kept outside
this repo. If this file disagrees with it, that doc wins.** This file carries
only what an executor session needs to start.

## Branch tips

| Branch | Tip | Role |
|---|---|---|
| `main` | `85b8549` (later commits on `main` are docs-only) | the comment-posting fix, merged |
| `fix-like-path-and-idle-waits` | `ca2f27e` | on top of `main`: the like path + bounded waits |
| `handle-unavailable-posts` | `50dbc60` | on top of `fix-like-path-and-idle-waits`: dead-post classifier + `UNAVAILABLE` |

**`handle-unavailable-posts` is the running branch.** Run the tool from it.

## Next action

1. Run **Dispatch 15**.
2. Then a **live batch of 5** comments from `handle-unavailable-posts`, checked
   on LinkedIn itself.

## Read before acting

`docs/PRINCIPLES.md`, `docs/ARCHITECTURE.md`, `docs/MAINTENANCE.md` (§6–§7).

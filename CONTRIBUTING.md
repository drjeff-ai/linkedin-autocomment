# Contributing / Development

Thanks for your interest. This is a personal-use project (see the disclaimer in
[README.md](README.md)), but the development workflow is straightforward.

## Setup

```bash
uv venv
uv pip install -r requirements.txt
uv pip install -r requirements-dev.txt   # pytest, pytest-cov, ruff
```

Python 3.12 on Windows is the reference environment (Selenium drives your real
Chrome). The core library code is cross-platform, but the `.bat` launchers and
the Windows Task Scheduler notes are Windows-specific.

## Project layout

The application is a package, `linkedin_automation/` (the dashboard is
`python -m linkedin_automation.dashboard`); standalone maintenance scripts live
in `tools/`. See the tree in [README.md](README.md#project-structure).

- **Run everything from the project root.** The dashboard shells out to package
  modules as `python -m linkedin_automation.<module>`; `tools/` scripts are run
  as `python tools/<script>.py` and each prepends the project root to
  `sys.path` so `import linkedin_automation` resolves.
- **Imports**: inside the package use relative imports (`from . import
  post_store`); tests and `tools/` import the package absolutely
  (`from linkedin_automation import post_store`).
- **Paths**: never build data paths from a module's `__file__`. Runtime data
  belongs under `data/` at the project root — use `profile_manager`'s
  `PROJECT_ROOT` / `DATA_ROOT` / `get_data_dir()` helpers.

## Running tests

```bash
uv run pytest tests/ -v        # full suite
uv run pytest tests/test_ad_filter.py -v   # a single file
```

Every test is offline: **no network, no browser, no OpenAI calls.** External
edges (Selenium, the OpenAI client, the filesystem, the clipboard) are mocked or
faked. If you add a feature that touches one of those, mock it the same way — a
test that needs real credentials or a live browser will not be accepted.

Test-fixture data must be **synthetic**. Do not paste real people's names,
profile URLs, or post text from a live feed into a fixture — invent them, or use
clearly public figures. (Company/brand examples used to exercise the ad filter
are fine.)

## Linting

```bash
uv run ruff check .            # must be clean before committing
```

## Conventions

- **Work on a feature branch**, one per unit of work. Never commit directly to
  `main`.
- **Stage files explicitly** (`git add <path>`), never `git add .`, and confirm
  `.gitignore` still covers everything under `data/`, `.env*`, and the HTML
  dumps before staging.
- **Commit messages**: start with the branch/feature name, then a short summary,
  then a body explaining *what changed and why*. Reference the behavior, not just
  the files.
- **Docstrings**: every module, public function, public class, and public method
  needs a docstring. Keep them short and about intent.
- **New code needs tests** in the same change; a failing suite blocks the commit.
- **Dependencies**: prefer the standard library. Pin any new dependency in
  `requirements.txt` (or `requirements-dev.txt`) — no unpinned installs.

## Secrets

Never hardcode API keys, tokens, or credentials — read them from environment
variables. Never commit `.env`, `data/`, or anything with a real session in it.
If a new secret is needed, add its **name** (no value) to `.env.example` and
document it in the README.

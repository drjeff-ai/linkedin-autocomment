"""Two platforms, two stores, one profile — and no leakage between them.

The store split is what makes a second platform possible at all, so these tests
assert the property the design rests on: a ``(profile, platform)`` pair addresses
its OWN file, and mutating one store cannot be observed from the other.

The strongest assertion here is ``test_linkedin_file_is_byte_identical_after_x_
mutations``. Comparing counts would pass even if the two stores shared a file and
happened not to collide on keys; comparing bytes cannot.

The last test guards the test infrastructure rather than the store: it fails if
``conftest``'s ``get_data_dir`` stub ever stops honouring ``subdir`` and silently
collapses both platforms onto one path again.
"""

import pytest

from linkedin_automation import post_store
from linkedin_automation import profile_manager as pm

PROFILE = "testprofile"


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    """Point DATA_ROOT at a temp dir so real profile data is never touched."""
    root = tmp_path / "data"
    monkeypatch.setattr(pm, "DATA_ROOT", str(root))
    return root


def make_post(url, author="Author", text="a post"):
    """A finder-shaped post dict."""
    return {"url": url, "author_name": author, "text": text, "relevance_score": 7}


LI_URL = "https://www.linkedin.com/feed/update/urn:li:activity:1/"
X_URL_1 = "https://x.com/someone/status/1"
X_URL_2 = "https://x.com/someone/status/2"


# ─── Path derivation ──────────────────────────────────────────────────────────

def test_linkedin_path_is_unchanged_by_the_platform_split(data_root):
    """LinkedIn keeps data/<profile>/posts_db.json — the working side never moved.

    This is the whole reason the split is cheap: no migration, no data movement,
    so an existing install is unaffected.
    """
    path = post_store.PostStore._default_path(PROFILE, post_store.LINKEDIN)
    assert path == str(data_root / PROFILE / "posts_db.json")


def test_platform_defaults_to_linkedin(data_root):
    """Every pre-existing caller passes no platform and must get its old store."""
    assert (post_store.PostStore._default_path(PROFILE)
            == post_store.PostStore._default_path(PROFILE, post_store.LINKEDIN))


def test_a_non_linkedin_platform_gets_its_own_subdirectory(data_root):
    path = post_store.PostStore._default_path(PROFILE, post_store.X)
    assert path == str(data_root / PROFILE / "x" / "posts_db.json")


def test_two_platforms_resolve_to_different_paths(data_root):
    assert (post_store.PostStore._default_path(PROFILE, post_store.LINKEDIN)
            != post_store.PostStore._default_path(PROFILE, post_store.X))


def test_an_empty_platform_falls_back_to_linkedin(data_root):
    """A caller passing None/"" must not silently create a store at a junk path."""
    for empty in (None, ""):
        assert (post_store.PostStore._default_path(PROFILE, empty)
                == post_store.PostStore._default_path(PROFILE, post_store.LINKEDIN))


def test_the_store_records_its_own_platform(data_root):
    assert post_store.PostStore(PROFILE).platform == post_store.LINKEDIN
    assert post_store.PostStore(PROFILE, platform=post_store.X).platform == post_store.X


def test_an_explicit_path_still_wins_over_platform(tmp_path, data_root):
    """The injectable path must keep working — tools/reconcile_bins.py relies on it."""
    explicit = str(tmp_path / "custom.json")
    store = post_store.PostStore(PROFILE, path=explicit, platform=post_store.X)
    assert store.path == explicit


# ─── Independent read/write ───────────────────────────────────────────────────

def test_each_platform_reads_back_only_its_own_posts(data_root):
    li = post_store.PostStore(PROFILE, platform=post_store.LINKEDIN)
    x = post_store.PostStore(PROFILE, platform=post_store.X)

    li.upsert_scraped(make_post(LI_URL), save=True)
    x.upsert_scraped(make_post(X_URL_1), save=True)
    x.upsert_scraped(make_post(X_URL_2), save=True)

    # Re-read from disk: in-memory state proves nothing about file isolation.
    li_reread = post_store.PostStore(PROFILE, platform=post_store.LINKEDIN)
    x_reread = post_store.PostStore(PROFILE, platform=post_store.X)

    assert li_reread.counts()["NEW"] == 1
    assert x_reread.counts()["NEW"] == 2
    assert LI_URL in li_reread.posts
    assert not any("x.com" in key for key in li_reread.posts)
    assert not any("linkedin.com" in key for key in x_reread.posts)


def test_both_store_files_exist_separately(data_root):
    li = post_store.PostStore(PROFILE, platform=post_store.LINKEDIN)
    x = post_store.PostStore(PROFILE, platform=post_store.X)
    li.upsert_scraped(make_post(LI_URL), save=True)
    x.upsert_scraped(make_post(X_URL_1), save=True)

    import os
    assert os.path.exists(li.path)
    assert os.path.exists(x.path)
    assert li.path != x.path


def test_linkedin_file_is_byte_identical_after_x_mutations(data_root):
    """The load-bearing assertion: X's writes leave LinkedIn's file untouched.

    Byte comparison rather than counts — counts would still pass if the two
    stores shared a file and merely failed to collide on keys.
    """
    li = post_store.PostStore(PROFILE, platform=post_store.LINKEDIN)
    li.upsert_scraped(make_post(LI_URL), save=True)
    before = open(li.path, "rb").read()

    x = post_store.PostStore(PROFILE, platform=post_store.X)
    x.upsert_scraped(make_post(X_URL_1), save=True)
    x.upsert_scraped(make_post(X_URL_2), save=True)
    x.reject(X_URL_1, save=True)
    x.mark_generated(X_URL_2, "a reply draft", save=True)

    assert open(li.path, "rb").read() == before


def test_lifecycle_transitions_are_isolated_per_platform(data_root):
    li = post_store.PostStore(PROFILE, platform=post_store.LINKEDIN)
    li.upsert_scraped(make_post(LI_URL), save=True)
    x = post_store.PostStore(PROFILE, platform=post_store.X)
    x.upsert_scraped(make_post(X_URL_1), save=True)
    x.upsert_scraped(make_post(X_URL_2), save=True)
    x.reject(X_URL_1, save=True)
    x.mark_generated(X_URL_2, "a reply draft", save=True)

    li_counts = post_store.PostStore(PROFILE, platform=post_store.LINKEDIN).counts()
    x_counts = post_store.PostStore(PROFILE, platform=post_store.X).counts()

    assert li_counts == {"NEW": 1, "GENERATED": 0, "COMMENTED": 0, "TRASH": 0}
    assert x_counts == {"NEW": 0, "GENERATED": 1, "COMMENTED": 0, "TRASH": 1}


def test_the_same_url_in_both_stores_stays_two_independent_records(data_root):
    """post_key is URL-first and platform-agnostic; separation comes from the file.

    A cross-posted link could legitimately appear on both platforms. Each store
    must track its own lifecycle for it.
    """
    shared = "https://example.com/an-article"
    li = post_store.PostStore(PROFILE, platform=post_store.LINKEDIN)
    x = post_store.PostStore(PROFILE, platform=post_store.X)
    li.upsert_scraped(make_post(shared), save=True)
    x.upsert_scraped(make_post(shared), save=True)

    x.reject(shared, save=True)

    assert post_store.PostStore(PROFILE, platform=post_store.LINKEDIN).get(shared)["status"] == post_store.NEW
    assert post_store.PostStore(PROFILE, platform=post_store.X).get(shared)["status"] == post_store.TRASH


# ─── load_synced_store: reconciler injection ──────────────────────────────────

def test_a_non_linkedin_store_is_not_reconciled_by_default(data_root, monkeypatch):
    """X must not run LinkedIn's reconciler — it reads LinkedIn's derived files."""
    called = []
    monkeypatch.setattr(post_store, "reconcile",
                        lambda *a, **k: called.append(a) or {})

    store = post_store.load_synced_store(PROFILE, platform=post_store.X)

    assert called == []
    assert store.platform == post_store.X


def test_a_non_linkedin_store_is_not_seeded_from_legacy_files(data_root, monkeypatch):
    """migrate_from_legacy reads LinkedIn's ai_posts_*/comments_* — wrong for X."""
    called = []
    monkeypatch.setattr(post_store, "migrate_from_legacy",
                        lambda *a, **k: called.append(a) or {})

    post_store.load_synced_store(PROFILE, platform=post_store.X)

    assert called == []


def test_an_injected_reconciler_is_called_for_a_non_linkedin_store(data_root):
    seen = []

    def spy(profile_name=None, store=None, stats=None):
        seen.append((profile_name, store.platform))
        return store.counts()

    post_store.load_synced_store(PROFILE, platform=post_store.X, reconciler=spy)

    assert seen == [(PROFILE, post_store.X)]


def test_linkedin_still_reconciles_by_default(data_root, monkeypatch):
    """The working side keeps its behaviour: reconcile runs, unasked."""
    called = []
    monkeypatch.setattr(post_store, "reconcile",
                        lambda *a, **k: called.append(a) or {})

    store = post_store.load_synced_store(PROFILE)

    assert len(called) == 1
    assert store.platform == post_store.LINKEDIN


def test_an_injected_reconciler_overrides_linkedins_default(data_root, monkeypatch):
    monkeypatch.setattr(post_store, "reconcile",
                        lambda *a, **k: pytest.fail("default reconciler ran"))
    seen = []
    post_store.load_synced_store(
        PROFILE, platform=post_store.LINKEDIN,
        reconciler=lambda profile_name=None, store=None, stats=None: seen.append(1))

    assert seen == [1]


def test_a_synced_x_store_round_trips_its_own_data(data_root):
    x = post_store.PostStore(PROFILE, platform=post_store.X)
    x.upsert_scraped(make_post(X_URL_1), save=True)
    x.mark_generated(X_URL_1, "a reply draft", save=True)

    reloaded = post_store.load_synced_store(PROFILE, platform=post_store.X)

    assert reloaded.counts()["GENERATED"] == 1
    assert reloaded.get(X_URL_1)["comment"] == "a reply draft"


# ─── Test-infrastructure guard ────────────────────────────────────────────────

def test_the_api_client_fixture_keeps_platforms_on_separate_paths(comments_dir):
    """Regression guard for the conftest stub, not for the store.

    ``tests/conftest.py``'s ``comments_dir`` fixture stubs ``get_data_dir``. It
    previously accepted ``subdir`` and ignored it, so under that fixture — and
    therefore under ``api_client``, which every dashboard-endpoint test uses —
    both platforms resolved to the SAME file.

    Nothing failed at the time, because nothing requested a platform. The cost
    would have landed later: a platform-aware endpoint test asserting isolation
    would have passed against a shared store, proving nothing while looking
    green. This test fails if that stub ever regresses.
    """
    linkedin = post_store.PostStore._default_path(PROFILE, post_store.LINKEDIN)
    x = post_store.PostStore._default_path(PROFILE, post_store.X)

    assert linkedin != x, (
        "conftest's get_data_dir stub is ignoring `subdir` again: both platforms "
        "resolve to the same store file, so any isolation assertion under the "
        "api_client fixture would pass without testing anything."
    )
    assert x.endswith(f"x{__import__('os').sep}posts_db.json")

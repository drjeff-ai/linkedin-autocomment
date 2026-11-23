"""Test helper: approve, in the store, every draft a poster TXT file carries.

E0 (DEC-060) made the poster re-check the store before each comment, so a test
that drives ``LinkedInCommentPoster.run`` over a hand-written TXT file must
first make those drafts real approved records, exactly as a human would through
the Review panel. It uses the real ``PostStore.approve``, not a stub of the gate.
"""
from linkedin_automation import post_store
from linkedin_automation.comment_poster import LinkedInCommentPoster


def approve_txt(profile_name, txt_path, parse=None):
    """Seed and approve each ``(url, comment)`` in ``txt_path``. Returns the path.

    ``parse`` is the parser the poster will use, when a test replaced it.
    """
    store = post_store.PostStore(profile_name=profile_name)
    parse = parse or LinkedInCommentPoster.parse_comments_file
    for c in parse(str(txt_path)):
        store.mark_generated(c["url"], c["comment"])
        assert store.approve(c["url"]), c["url"]
    store.save()
    return txt_path

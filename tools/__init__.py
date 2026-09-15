"""Standalone maintenance and diagnostic scripts.

These are launched directly (e.g. ``python tools/reconcile_bins.py``) or, for
``login_check``, imported by the test suite. Each script inserts the project
root onto ``sys.path`` so ``import linkedin_automation`` resolves no matter the
launch directory.
"""

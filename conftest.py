"""Root conftest.

Its mere presence makes pytest add the project root to sys.path (prepend import
mode), so tests under tests/ can `import linkedin_automation.profile_manager` and the other
top-level modules directly.
"""

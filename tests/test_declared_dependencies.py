"""Every third-party module the package imports must be declared in requirements.txt.

**The bug this exists for.** ``post_generator``'s "react to an article" mode
imports ``requests`` and ``bs4``, and requirements.txt declared neither.
``requests`` happened to be installed as a transitive dependency of other
packages, and ``bs4`` was simply absent — so the feature raised
ModuleNotFoundError on any clean install, including a fresh checkout of this
repo. The imports are inside the function, so nothing failed until a user
actually pressed Generate from Article.

An import test alone would not have caught it: the module imports fine, and on a
developer machine with the package incidentally present it would pass. This
compares what the source imports against what requirements.txt promises, which is
the thing that was actually wrong.
"""

import ast
import os
import re
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE_DIR = os.path.join(REPO_ROOT, "linkedin_automation")
REQUIREMENTS = os.path.join(REPO_ROOT, "requirements.txt")

# Import name -> distribution name, where they differ.
IMPORT_TO_DISTRIBUTION = {
    "bs4": "beautifulsoup4",
    "dotenv": "python-dotenv",
    "webdriver_manager": "webdriver-manager",
}

# Shipped with Python, so never declared.
STDLIB = set(sys.stdlib_module_names)


def _declared_distributions():
    """Distribution names from requirements.txt, lowercased, comments stripped."""
    out = set()
    with open(REQUIREMENTS, encoding="utf-8") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            name = re.split(r"[=<>!~\[]", line, 1)[0].strip().lower()
            if name:
                out.add(name)
    return out


def _imported_top_level_modules():
    """Every top-level module imported anywhere in the package, at any nesting."""
    found = set()
    for dirpath, dirnames, filenames in os.walk(PACKAGE_DIR):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            path = os.path.join(dirpath, filename)
            with open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read(), filename=path)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        found.add(alias.name.split(".")[0])
                elif isinstance(node, ast.ImportFrom):
                    # level > 0 is a relative import — our own package.
                    if node.level == 0 and node.module:
                        found.add(node.module.split(".")[0])
    return found


def test_every_third_party_import_is_declared():
    declared = _declared_distributions()
    undeclared = []
    for module in sorted(_imported_top_level_modules()):
        if module in STDLIB or module == "linkedin_automation":
            continue
        distribution = IMPORT_TO_DISTRIBUTION.get(module, module).lower()
        if distribution not in declared:
            undeclared.append(f"{module} (needs '{distribution}' in requirements.txt)")
    assert not undeclared, "undeclared third-party imports: " + ", ".join(undeclared)


@pytest.mark.parametrize("distribution", ["requests", "beautifulsoup4"])
def test_the_article_mode_dependencies_are_declared(distribution):
    """Named explicitly so a regression points straight at the feature that broke."""
    assert distribution in _declared_distributions()


def test_requirements_are_pinned():
    """CLAUDE.md: pin versions. An unpinned dep is a future silent break."""
    unpinned = []
    with open(REQUIREMENTS, encoding="utf-8") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if line and "==" not in line:
                unpinned.append(line)
    assert not unpinned, f"unpinned requirements: {unpinned}"

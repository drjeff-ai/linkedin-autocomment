"""linkedin_automation/dom_probe.py — count selector matches in saved HTML, offline.

`selector_health` needs to answer one question per selector: how many elements
does it match? Against a live page Selenium answers it inside the browser.
Against a **saved** page there is no browser, and that is the case this module
exists for: it is what makes the selector gate runnable in CI, on a fixture,
with no LinkedIn session and no Chrome.

**What an offline check does and does not prove.** A fixture is a frozen
snapshot of a DOM shape that was once correct. Counting against it proves *the
selector constant in the code still matches the shape it was written for* — so
it catches a regression where someone edits a selector and breaks the match, and
it catches it deterministically, in CI, in milliseconds. It does **not** prove
the selector still matches **today's live LinkedIn**: only the live check
(`--post-url` / the feed run) can tell you LinkedIn changed its DOM. The two
failures are different and both checks are needed. See ARCHITECTURE.md §8.3.

Deliberately a SUBSET of CSS and XPath, not a general engine.

The whole point of a selector watchdog is that it never reports a clean result
for something it did not actually test. A permissive engine that quietly returns
0 for a selector it failed to understand would produce exactly the false
all-clear this project already lived through (the posting path reported HEALTHY
while every post-detail selector was dead — see ARCHITECTURE.md §8.2). So every
construct outside the supported grammar raises :class:`UnsupportedSelector`. A
loud "I cannot check this" is a usable result. A silent zero is not.

``tests/test_dom_probe.py`` asserts that every selector in
``selector_health.SELECTOR_REGISTRY`` parses, so a new selector using a
construct the grammar lacks breaks the build instead of degrading the check.

Pure stdlib: no Selenium, no lxml, no BeautifulSoup. It must be importable in a
context that has no browser at all.

Supported CSS
-------------
- selector lists: ``a, b``
- combinators: descendant (space), child ``>``, adjacent ``+``, sibling ``~``
- type ``div``, universal ``*``, class ``.x``, id ``#x``
- attributes: ``[a]``, ``[a='v']``, ``[a*='v']``, ``[a^='v']``, ``[a$='v']``,
  ``[a~='v']``, ``[a|='v']``
- ``:not(<compound>)``, where the argument has no combinator

Supported XPath
---------------
- ``//tag``
- ``//tag[normalize-space(.)='text']``
- ``//tag[@attr='value']``

That XPath list looks thin because the codebase uses exactly one XPath, and it
matters: ``LinkedInCommentPoster.SUBMIT_BUTTON_XPATH`` finds the comment submit
button by visible text. Counting it with CSS is impossible, and skipping it
would leave the highest-consequence selector in the project unchecked.
"""

import re
from html.parser import HTMLParser
from typing import Dict, List, Optional, Union


class UnsupportedSelector(ValueError):
    """Raised when a selector uses a construct this engine does not implement.

    Never caught and converted to a count of 0. A caller that cannot check a
    selector must say so.
    """


# HTML void elements never have children, so they are closed at their start tag.
VOID_ELEMENTS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
})

# Content that is text to the parser but never text to a human reader. Excluded
# from Node.text() so an XPath text match cannot be satisfied by a script body.
NON_RENDERED_TAGS = frozenset({"script", "style", "template", "noscript"})


class Node:
    """One element in a parsed document. Children are Nodes and raw text."""

    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs: Dict[str, str], parent: "Optional[Node]" = None):
        self.tag = tag
        self.attrs = attrs
        self.children: List[Union["Node", str]] = []
        self.parent = parent

    def classes(self) -> List[str]:
        return (self.attrs.get("class") or "").split()

    def text(self) -> str:
        """Concatenated descendant text, skipping non-rendered tags."""
        if self.tag in NON_RENDERED_TAGS:
            return ""
        out = []
        for child in self.children:
            out.append(child if isinstance(child, str) else child.text())
        return "".join(out)

    def walk(self):
        """Yield this node and every descendant element, in document order."""
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"<Node {self.tag} {self.attrs}>"


class _DomBuilder(HTMLParser):
    """Build a Node tree. Tolerant of unclosed tags, which real dumps contain."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#document", {})
        self._stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {k: (v if v is not None else "") for k, v in attrs}, self._stack[-1])
        self._stack[-1].children.append(node)
        if tag not in VOID_ELEMENTS:
            self._stack.append(node)

    def handle_startendtag(self, tag, attrs):
        node = Node(tag, {k: (v if v is not None else "") for k, v in attrs}, self._stack[-1])
        self._stack[-1].children.append(node)

    def handle_endtag(self, tag):
        # Close the nearest matching open element. An end tag with no matching
        # start (malformed markup, or a fragment that begins mid-tree) is
        # ignored rather than unwinding the whole stack.
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i].tag == tag:
                del self._stack[i:]
                return

    def handle_data(self, data):
        self._stack[-1].children.append(data)


def parse_html(html: str) -> Node:
    """Parse ``html`` into a Node tree and return the synthetic document root."""
    builder = _DomBuilder()
    builder.feed(html)
    builder.close()
    return builder.root


# ─── CSS ──────────────────────────────────────────────────────────────────────

_SIMPLE_RE = re.compile(
    r"""
      (?P<universal>\*)
    | (?P<tag>[A-Za-z][A-Za-z0-9_-]*)
    | \.(?P<cls>[A-Za-z0-9_-]+)
    | \#(?P<id>[A-Za-z0-9_-]+)
    | \[\s*(?P<attr>[A-Za-z0-9_:.-]+)\s*
        (?:(?P<op>[*^$~|]?=)\s*(?P<val>"[^"]*"|'[^']*'|[^\]\s]+)\s*)?\]
    | :not\((?P<neg>[^()]*)\)
    """,
    re.VERBOSE,
)

_COMBINATORS = frozenset({" ", ">", "+", "~"})


def _split_top_level(selector: str, delimiters) -> List[str]:
    """Split on ``delimiters`` that sit outside brackets and parentheses."""
    parts, buf, depth = [], [], 0
    for ch in selector:
        if ch in "[(":
            depth += 1
        elif ch in "])":
            depth -= 1
        if depth == 0 and ch in delimiters:
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    parts.append("".join(buf))
    return parts


def _parse_compound(chunk: str, original: str) -> List[tuple]:
    """Parse one compound selector into a list of (kind, value) conditions."""
    chunk = chunk.strip()
    if not chunk:
        raise UnsupportedSelector(f"empty compound in {original!r}")
    conditions, pos = [], 0
    while pos < len(chunk):
        match = _SIMPLE_RE.match(chunk, pos)
        if not match:
            raise UnsupportedSelector(
                f"cannot parse {chunk[pos:]!r} in {original!r}"
            )
        pos = match.end()
        if match.group("universal"):
            conditions.append(("universal", None))
        elif match.group("tag"):
            conditions.append(("tag", match.group("tag").lower()))
        elif match.group("cls"):
            conditions.append(("class", match.group("cls")))
        elif match.group("id"):
            conditions.append(("id", match.group("id")))
        elif match.group("attr"):
            value = match.group("val")
            if value and value[0] in "\"'":
                value = value[1:-1]
            conditions.append(("attr", (match.group("attr").lower(),
                                        match.group("op"), value)))
        else:
            negated = _parse_compound(match.group("neg"), original)
            conditions.append(("not", negated))
    return conditions


def _parse_complex(selector: str) -> List[tuple]:
    """Parse one complex selector into ``[(combinator|None, compound), ...]``."""
    tokens, buf, depth = [], [], 0
    for ch in selector.strip():
        if ch in "[(":
            depth += 1
        elif ch in "])":
            depth -= 1
        if depth == 0 and (ch.isspace() or ch in ">+~"):
            tokens.append("".join(buf))
            buf = []
            tokens.append(" " if ch.isspace() else ch)
            continue
        buf.append(ch)
    tokens.append("".join(buf))

    # Collapse runs of whitespace/explicit combinators: "a > b" is three tokens
    # of separator between two compounds, and only the explicit one counts.
    parts, pending = [], None
    for token in tokens:
        if token == "":
            continue
        if token in _COMBINATORS:
            if token == " ":
                if pending is None:
                    pending = " "
            else:
                if pending is not None and pending != " ":
                    raise UnsupportedSelector(
                        f"two combinators in a row in {selector!r}")
                pending = token
            continue
        if not parts and pending is not None:
            raise UnsupportedSelector(f"selector starts with a combinator: {selector!r}")
        parts.append((pending, _parse_compound(token, selector)))
        pending = None
    if pending is not None:
        raise UnsupportedSelector(f"selector ends with a combinator: {selector!r}")
    if not parts:
        raise UnsupportedSelector(f"empty selector: {selector!r}")
    return parts


def parse_css(selector: str) -> List[List[tuple]]:
    """Parse a selector list. Raises UnsupportedSelector on anything unsupported."""
    if not selector or not selector.strip():
        raise UnsupportedSelector("empty selector")
    return [_parse_complex(part) for part in _split_top_level(selector, {","})]


def _attr_matches(node: Node, name: str, op: Optional[str], value: Optional[str]) -> bool:
    actual = node.attrs.get(name)
    if actual is None:
        return False
    if op is None:
        return True
    if op == "=":
        return actual == value
    if op == "*=":
        return bool(value) and value in actual
    if op == "^=":
        return bool(value) and actual.startswith(value)
    if op == "$=":
        return bool(value) and actual.endswith(value)
    if op == "~=":
        return bool(value) and value in actual.split()
    if op == "|=":
        return actual == value or actual.startswith(f"{value}-")
    raise UnsupportedSelector(f"attribute operator {op!r}")


def _match_compound(node: Node, conditions: List[tuple]) -> bool:
    for kind, value in conditions:
        if kind == "universal":
            continue
        if kind == "tag":
            if node.tag != value:
                return False
        elif kind == "class":
            if value not in node.classes():
                return False
        elif kind == "id":
            if node.attrs.get("id") != value:
                return False
        elif kind == "attr":
            if not _attr_matches(node, *value):
                return False
        elif kind == "not":
            if _match_compound(node, value):
                return False
    return True


def _previous_element_sibling(node: Node) -> Optional[Node]:
    parent = node.parent
    if parent is None:
        return None
    previous = None
    for child in parent.children:
        if child is node:
            return previous
        if isinstance(child, Node):
            previous = child
    return None


def _match_complex(node: Node, parts: List[tuple], index: int) -> bool:
    combinator, compound = parts[index]
    if not _match_compound(node, compound):
        return False
    if index == 0:
        return True
    if combinator == ">":
        parent = node.parent
        return parent is not None and _match_complex(parent, parts, index - 1)
    if combinator == "+":
        sibling = _previous_element_sibling(node)
        return sibling is not None and _match_complex(sibling, parts, index - 1)
    if combinator == "~":
        sibling = _previous_element_sibling(node)
        while sibling is not None:
            if _match_complex(sibling, parts, index - 1):
                return True
            sibling = _previous_element_sibling(sibling)
        return False
    # Descendant.
    ancestor = node.parent
    while ancestor is not None:
        if _match_complex(ancestor, parts, index - 1):
            return True
        ancestor = ancestor.parent
    return False


def select_css(root: Node, selector: str) -> List[Node]:
    """Return every element under ``root`` matching ``selector``, document order."""
    parsed = parse_css(selector)
    matches = []
    for node in root.walk():
        if node is root and root.tag == "#document":
            continue
        if any(_match_complex(node, parts, len(parts) - 1) for parts in parsed):
            matches.append(node)
    return matches


# ─── XPath (a deliberately tiny subset) ───────────────────────────────────────

_XPATH_TEXT_RE = re.compile(
    r"^//(?P<tag>\*|[A-Za-z][A-Za-z0-9_-]*)"
    r"\[normalize-space\(\.\)\s*=\s*(?P<quote>[\"'])(?P<text>.*?)(?P=quote)\]$"
)
_XPATH_ATTR_RE = re.compile(
    r"^//(?P<tag>\*|[A-Za-z][A-Za-z0-9_-]*)"
    r"\[@(?P<attr>[A-Za-z0-9_:.-]+)\s*=\s*(?P<quote>[\"'])(?P<value>.*?)(?P=quote)\]$"
)
_XPATH_BARE_RE = re.compile(r"^//(?P<tag>\*|[A-Za-z][A-Za-z0-9_-]*)$")


def _normalize_space(text: str) -> str:
    return " ".join(text.split())


def select_xpath(root: Node, expression: str) -> List[Node]:
    """Return every element matching a supported XPath expression."""
    expression = (expression or "").strip()

    match = _XPATH_TEXT_RE.match(expression)
    if match:
        tag, wanted = match.group("tag"), match.group("text")
        return [n for n in root.walk()
                if (tag == "*" or n.tag == tag.lower())
                and _normalize_space(n.text()) == wanted]

    match = _XPATH_ATTR_RE.match(expression)
    if match:
        tag, attr, value = match.group("tag"), match.group("attr").lower(), match.group("value")
        return [n for n in root.walk()
                if (tag == "*" or n.tag == tag.lower()) and n.attrs.get(attr) == value]

    match = _XPATH_BARE_RE.match(expression)
    if match:
        tag = match.group("tag")
        return [n for n in root.walk()
                if n.tag != "#document" and (tag == "*" or n.tag == tag.lower())]

    raise UnsupportedSelector(f"unsupported XPath: {expression!r}")


# ─── The interface the watchdog actually uses ─────────────────────────────────

def is_xpath(selector: str) -> bool:
    """True when ``selector`` is written in XPath rather than CSS.

    The single source of truth for that distinction. ``selector_health.selector_by``
    defers to this so the live and offline counters can never disagree about how
    to read a selector — a divergence there would mean the fixture check and the
    browser check were silently testing different things.

    Read off the selector's own shape: XPath is the only one that starts with
    ``/`` or ``(``. That keeps ``count_fn(selector)`` a one-argument contract, so
    registry entries need no per-entry ``by`` key.
    """
    return (selector or "").startswith(("//", "/", "("))


def make_counter(html: str):
    """Return ``count(selector) -> int`` bound to parsed ``html``.

    The offline stand-in for the live ``len(driver.find_elements(...))`` counter.
    Same one-argument signature, so ``check_registry`` runs unchanged against a
    fixture or a browser without knowing which it has — which is what makes the
    offline gate a real check of the same logic rather than a parallel one.

    Raises :class:`UnsupportedSelector` rather than returning 0 for a selector it
    cannot parse.
    """
    root = parse_html(html)

    def count(selector: str) -> int:
        nodes = (select_xpath(root, selector) if is_xpath(selector)
                 else select_css(root, selector))
        return len(nodes)

    return count


def harvest_hooks(html_or_root) -> Dict[str, List[str]]:
    """Collect the stable-looking hooks present in a document.

    These are the replacement candidates a repair starts from: LinkedIn ships
    hashed class names that change between deploys, but ``data-testid`` and
    ``data-view-name`` have survived every break this project has seen.
    """
    root = html_or_root if isinstance(html_or_root, Node) else parse_html(html_or_root)
    testids, view_names, aria_labels, class_tokens = set(), set(), set(), set()
    for node in root.walk():
        if node.attrs.get("data-testid"):
            testids.add(node.attrs["data-testid"])
        if node.attrs.get("data-view-name"):
            view_names.add(node.attrs["data-view-name"])
        if node.attrs.get("aria-label") and node.tag == "button":
            aria_labels.add(node.attrs["aria-label"])
        class_tokens.update(node.classes())
    return {
        "data_testids": sorted(testids),
        "data_view_names": sorted(view_names),
        "button_aria_labels": sorted(aria_labels),
        "class_tokens": sorted(class_tokens),
    }


def candidate_selectors(hooks: Dict[str, List[str]]) -> List[str]:
    """Turn harvested hooks into pasteable candidate selectors."""
    out = [f"[data-view-name='{v}']" for v in hooks.get("data_view_names", [])]
    out += [f"[data-testid='{t}']" for t in hooks.get("data_testids", [])]
    out += [f"button[aria-label='{a}']" for a in hooks.get("button_aria_labels", [])]
    return out

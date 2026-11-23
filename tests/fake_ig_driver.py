"""A fake Selenium driver over the synthetic Instagram fixtures (D-027 E3).

Pages are the HTML files in ``tests/fixtures/instagram/``, parsed by
``dom_probe`` (the offline selector engine the fixture gate uses), so the
adapter's selectors are matched exactly as the registry check matches them.
Nothing opens a socket or a browser.

Behaviour, each switchable so a verifier can be shown both ways:

* clicking the action bar's Like turns it into Unlike (``like_works``);
* clicking the form's Post adds a comment by ``handle`` with the typed text to
  the thread (``submit_works``; ``submit_text`` posts different text). The
  "server" keeps it, so it is there again after a reload, unless
  ``server_keeps`` is False (Instagram's optimistic echo of a comment it then
  drops);
* ``send_keys`` appends to a textarea's value.

It declares ``IG_OFFLINE_FIXTURE = True``, which is what tells the adapter it
is not a live browser. :class:`LiveLookingDriver` is the same fake without
that marker, to prove the live gate.
"""

import os

from selenium.webdriver.common.by import By

from linkedin_automation import dom_probe

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures", "instagram")
BASE = "https://www.instagram.com"


def fixture(name):
    with open(os.path.join(FIXTURES, name + ".html"), encoding="utf-8") as f:
        return f.read()


def _select(root, by, selector, include_root):
    if by == By.XPATH:
        nodes = dom_probe.select_xpath(root, selector)
    elif by == By.CSS_SELECTOR:
        nodes = dom_probe.select_css(root, selector)
    else:
        raise NotImplementedError(by)
    if not include_root:
        nodes = [n for n in nodes if n is not root]
    return nodes


def _ancestor(node, pred):
    while node is not None:
        if pred(node):
            return node
        node = node.parent
    return None


class FakeElement:
    def __init__(self, driver, node):
        self.driver = driver
        self.node = node

    @property
    def tag_name(self):
        return self.node.tag

    @property
    def text(self):
        return " ".join(self.node.text().split())

    def get_attribute(self, name):
        return self.node.attrs.get(name)

    def find_elements(self, by, selector):
        return [FakeElement(self.driver, n)
                for n in _select(self.node, by, selector, include_root=False)]

    def is_displayed(self):
        return True

    def is_enabled(self):
        return "disabled" not in self.node.attrs

    def send_keys(self, text):
        self.driver.typed.append(text)
        self.node.attrs["value"] = (self.node.attrs.get("value") or "") + text

    def click(self):
        self.driver.clicks.append(self.node)
        self.driver.on_click(self.node)


class FakeIGDriver:
    IG_OFFLINE_FIXTURE = True

    def __init__(self, pages, handle="dev_tester", like_works=True, submit_works=True,
                 submit_text=None, server_keeps=True):
        #: url -> fixture name (or raw html)
        self.pages = dict(pages)
        self.handle = handle
        self.like_works = like_works
        self.submit_works = submit_works
        self.submit_text = submit_text
        self.server_keeps = server_keeps
        #: url -> comment texts the "server" kept, drawn again on every load
        self.kept = {}
        self.current_url = "about:blank"
        self.root = dom_probe.parse_html("<html><body></body></html>")
        self.html = ""
        self.visits = []
        self.clicks = []
        self.typed = []
        self.scripts = []
        self.title = ""

    # ── navigation ─────────────────────────────────────────────────────────
    def get(self, url):
        self.visits.append(url)
        self.current_url = url
        src = self.pages.get(url)
        if src is None:
            self.html = "<html><body></body></html>"   # never renders: a timeout
        else:
            self.html = src if src.lstrip().startswith("<") else fixture(src)
        self.root = dom_probe.parse_html(self.html)
        for text in self.kept.get(url, []):
            self._add_comment(text)

    def find_elements(self, by, selector):
        return [FakeElement(self, n) for n in _select(self.root, by, selector, True)]

    def find_element(self, by, selector):
        found = self.find_elements(by, selector)
        if not found:
            raise LookupError(selector)
        return found[0]

    @property
    def page_source(self):
        return self.html

    def execute_script(self, script, *args):
        self.scripts.append(script)
        return None

    def save_screenshot(self, path):
        return False

    # ── behaviour ──────────────────────────────────────────────────────────
    def on_click(self, node):
        button = _ancestor(node, lambda n: n.attrs.get("role") == "button"
                           or n.tag == "button")
        if button is None:
            return
        section = _ancestor(button, lambda n: n.tag == "section")
        if section is not None:
            for svg in dom_probe.select_css(button, "svg[aria-label='Like']"):
                if self.like_works:
                    svg.attrs["aria-label"] = "Unlike"
            return
        form = _ancestor(button, lambda n: n.tag == "form")
        if form is not None and " ".join(button.text().split()) == "Post":
            if not self.submit_works:
                return
            box = dom_probe.select_css(form, "textarea")
            text = self.submit_text if self.submit_text is not None else \
                (box[0].attrs.get("value") if box else "")
            self._add_comment(text or "")
            if self.server_keeps:
                self.kept.setdefault(self.current_url, []).append(text or "")
            if box:
                box[0].attrs["value"] = ""

    def _add_comment(self, text):
        lists = dom_probe.select_css(self.root, "article ul")
        if not lists:
            return
        ul = lists[0]
        li = dom_probe.Node("li", {}, ul)
        a = dom_probe.Node("a", {"href": "/%s/" % self.handle}, li)
        a.children.append(self.handle)
        span = dom_probe.Node("span", {"dir": "auto"}, li)
        span.children.append(text)
        li.children.extend([a, span])
        ul.children.append(li)


class LiveLookingDriver(FakeIGDriver):
    """The same fake with no offline marker: the adapter must treat it as a
    live browser."""
    IG_OFFLINE_FIXTURE = False


class Hands:
    """Plain clicks and typing (the live ``HumanHands`` moves a mouse)."""

    def click(self, driver, element):
        element.click()

    def type(self, driver, element, text):
        element.click()
        element.send_keys(text)


def post(code):
    return "%s/p/%s/" % (BASE, code)


def profile(handle):
    return "%s/%s/" % (BASE, handle)

"""The suite cannot reach the network (D-027 Y1, conftest ``_no_real_network``).

Each test below makes a real outbound attempt through a different layer and
asserts it is refused before any packet leaves. If one of these passes without
the guard firing, the suite is not hermetic.
"""

import socket
import urllib.request

from contextlib import contextmanager

import pytest


@contextmanager
def refused():
    """The guard's exception, matched by name: conftest is imported under more
    than one module name, so the class object is not a stable handle."""
    with pytest.raises(BaseException) as info:
        yield
    assert type(info.value).__name__ == "RealNetworkAccess", repr(info.value)


def test_a_raw_socket_connect_is_refused():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with refused():
            s.connect(("203.0.113.10", 443))
    finally:
        s.close()


def test_a_name_lookup_is_refused():
    with refused():
        socket.getaddrinfo("www.googleapis.com", 443)


def test_urllib_is_refused():
    with refused():
        urllib.request.urlopen("https://www.googleapis.com/", timeout=1)


def test_a_real_google_client_call_is_refused():
    """The real youtube_api path, with a real (static) service object: the
    request is built and sent, and the guard stops it at the socket. It cannot
    be swallowed by youtube_api's own ``except Exception``."""
    from google.oauth2.credentials import Credentials

    from linkedin_automation import youtube_api
    service = youtube_api.build_service(Credentials(token="not-a-real-token"))
    with refused():
        youtube_api.search_channels(service, "anything")


def test_loopback_stays_open():
    """Local sockets are not the network; the guard must not break them."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    cli = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        cli.connect(srv.getsockname())
    finally:
        cli.close()
        srv.close()

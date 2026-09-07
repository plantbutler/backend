"""Getting a message out of the house: ntfy, and the dead man's ping.

Neither of these may raise. An alert that cannot be delivered is something to
try again on the next tick; a service that falls over because a push provider
is down is a garden nobody is watering.
"""

import http.client
import time
import urllib.request
from collections.abc import Callable
from typing import NamedTuple

from . import constants


def hhmm(ts: int) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


class Alert(NamedTuple):
    """One message for the phone plus the write that remembers it went out.

    A None `message` is a silent judgement (a dose that worked): the record
    step still runs and nothing is posted. `record` is applied only after a
    successful send, in its own short transaction.
    """

    key: str | None  # alerts-table key; None for the unrecorded up-probe
    priority: str  # ntfy priority: 'high' | 'default' | 'min'
    tags: str  # ntfy Tags header: emoji shortcodes
    message: str | None
    record: Callable | None = None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # urllib follows a 301/302 by replaying the POST as a bodyless GET; on a
    # to-HTTPS-redirecting proxy that "succeeds" while the message is
    # dropped. A redirect here is a misconfiguration: fail it loudly.
    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def post_ntfy(base_url: str, topic: str, alert: Alert) -> bool:
    """One message to ntfy, True only on a 2xx. Never raises — alerting must
    never take the service down — and a False is retried on a later tick."""
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/{topic}",
        data=(alert.message or "").encode("utf-8"),
        method="POST",
        headers={
            "Title": "Plant Butler",
            "Priority": alert.priority,
            "Tags": alert.tags,
            "User-Agent": "plantbutler-backend",
        },
    )
    try:
        with _OPENER.open(request, timeout=constants.NTFY_TIMEOUT_S) as answer:
            return 200 <= answer.status < 300
    except (OSError, http.client.HTTPException, ValueError):
        return False


def ping_deadman(url: str) -> bool:
    """GET the dead-man URL; True on a 2xx. The same never-raise contract."""
    try:
        with _OPENER.open(url, timeout=constants.NTFY_TIMEOUT_S) as answer:
            return 200 <= answer.status < 300
    except (OSError, http.client.HTTPException, ValueError):
        return False

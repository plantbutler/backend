"""What the environment says, read once, and the refusals to start.

Every knob is an environment variable with a keyword override for the tests,
and the reading happens here rather than in `create_app` so that the refusals
sit beside the values they are about. They are refusals rather than defaults on
purpose: a butler that starts with no token, with a command TTL a live board
can outlive, or with its database in the container's own layer looks healthy
and is not, and the day you find out is the day the readings are gone.

Nothing here touches the disk apart from asking whether /data is a mount.
"""

import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from . import constants, notify, species, wire


class Config(NamedTuple):
    """The whole configuration, frozen, built by `configure` and passed on.

    `send`, `check` and `ping` are the three ways out of the house: the
    message, the reachability probe a quiet pass needs, and the dead man.
    Each is None when nothing configured it, which is a working butler with
    its alerting off rather than an error.
    """

    db: Path
    photos: Path
    secret: str
    interval: int
    cmd_ttl: int
    quiet_window: tuple[int, int]
    silent_after: int
    beat: float
    alerts_on: bool
    send: Callable[[notify.Alert], bool] | None
    check: Callable[[], bool] | None
    ping: Callable[[], bool] | None
    care_token: str
    get_json: Callable[[str], dict | None]


def env_int(given: int | None, name: str, default: str) -> int:
    raw = str(given) if given is not None else (os.environ.get(name) or default)
    try:
        return int(raw)
    except ValueError:
        raise ValueError(
            f"{name} must be an integer number of seconds, got {raw!r}"
        ) from None


def configure(
    db_path: str | None = None,
    token: str | None = None,
    next_s: int | None = None,
    cmd_ttl_s: int | None = None,
    quiet: str | None = None,
    ntfy_topic: str | None = None,
    ntfy_url: str | None = None,
    deadman_url: str | None = None,
    silent_s: int | None = None,
    tick_s: float | None = None,
    send: Callable[[notify.Alert], bool] | None = None,
    ping: Callable[[], bool] | None = None,
    probe: Callable[[], bool] | None = None,
    trefle_token: str | None = None,
    fetch: Callable[[str], dict | None] | None = None,
    photos_dir: str | None = None,
) -> Config:
    """Everything configurable comes from the environment, overridable for tests.

    Refusals to start, all of them loud and specific: a missing token (this
    listens on a LAN with other people's devices on it, and "forgot to set the
    token" must not be a working deployment); a BUTLER_NEXT_S or
    BUTLER_CMD_TTL_S that is not an integer; and a BUTLER_DB under /data when
    /data is not a mount, since a forgotten bind mount stores readings in the
    container layer and loses them on the next recreate while looking healthy.
    """
    db = Path(db_path or os.environ.get("BUTLER_DB", "/data/butler.db"))
    secret = token if token is not None else os.environ.get("BUTLER_TOKEN", "")
    if not secret:
        raise ValueError("BUTLER_TOKEN is not set; refusing to serve without one")

    interval = env_int(next_s, "BUTLER_NEXT_S", "60")
    cmd_ttl = env_int(cmd_ttl_s, "BUTLER_CMD_TTL_S", "900")
    if not constants.MIN_NEXT_S <= interval <= constants.MAX_NEXT_S:
        raise ValueError(
            f"BUTLER_NEXT_S out of range "
            f"({constants.MIN_NEXT_S}..{constants.MAX_NEXT_S}): {interval}"
        )
    if cmd_ttl < 2 * interval:
        # The TTL backstops are only safe if a live board always reports well
        # within the TTL; otherwise a 'sent' command can be swept aside and a
        # second one queued while the board still holds the first — two doses.
        raise ValueError(
            f"BUTLER_CMD_TTL_S ({cmd_ttl}) must be at least twice "
            f"BUTLER_NEXT_S ({interval}), or a live board could be declared "
            "dead between two on-time reports"
        )

    quiet_window = wire.parse_quiet(
        quiet if quiet is not None else os.environ.get("BUTLER_QUIET") or "22-08"
    )

    topic = (
        ntfy_topic
        if ntfy_topic is not None
        else os.environ.get("BUTLER_NTFY_TOPIC", "")
    )
    base_url = (
        ntfy_url
        if ntfy_url is not None
        else (os.environ.get("BUTLER_NTFY_URL") or "https://ntfy.sh")
    )
    deadman = (
        deadman_url
        if deadman_url is not None
        else os.environ.get("BUTLER_DEADMAN_URL", "")
    )
    silent_after = env_int(silent_s, "BUTLER_SILENT_S", str(constants.SILENT_AFTER_S))
    if not 60 <= silent_after <= 86400:
        raise ValueError(f"BUTLER_SILENT_S out of range (60..86400): {silent_after}")
    beat = tick_s if tick_s is not None else constants.ALERT_TICK_S
    alerts_on = bool(topic) or send is not None
    if deadman and not alerts_on:
        raise ValueError(
            "BUTLER_DEADMAN_URL is set but BUTLER_NTFY_TOPIC is not: the "
            "dead-man would report a healthy butler whose alerting is off"
        )
    check = probe
    if send is None and topic:

        def send(alert: notify.Alert) -> bool:
            return notify.post_ntfy(base_url, topic, alert)

        if check is None:

            def check() -> bool:
                # Reachability for quiet passes: a healthy garden sends no
                # messages, so without this an ntfy outage would never stop
                # the dead-man.
                return notify.ping_deadman(f"{base_url.rstrip('/')}/v1/health")

    if ping is None and deadman:

        def ping() -> bool:
            return notify.ping_deadman(deadman)

    if not alerts_on:
        print("BUTLER_NTFY_TOPIC unset: alerts are off", file=sys.stderr)

    care_token = (
        trefle_token
        if trefle_token is not None
        else os.environ.get("BUTLER_TREFLE_TOKEN", "")
    )
    get_json = fetch or species.fetch_json
    if not care_token and fetch is None:
        print("BUTLER_TREFLE_TOKEN unset: care lookups are typed in", file=sys.stderr)

    if db.parent == Path("/data") and not os.path.ismount("/data"):
        raise ValueError(
            "BUTLER_DB is under /data but /data is not a mounted volume; "
            "refusing to store readings in the container layer"
        )
    # Beside the database by default, so they land on the same bind mount and
    # are backed up or lost together — the one arrangement in which a restore
    # cannot produce rows whose files are from a different day.
    photos = Path(
        photos_dir or os.environ.get("BUTLER_PHOTOS") or str(db.parent / "photos")
    )
    if photos.parent == Path("/data") and not os.path.ismount("/data"):
        raise ValueError(
            "BUTLER_PHOTOS is under /data but /data is not a mounted volume; "
            "refusing to store photographs in the container layer"
        )

    return Config(
        db=db,
        photos=photos,
        secret=secret,
        interval=interval,
        cmd_ttl=cmd_ttl,
        quiet_window=quiet_window,
        silent_after=silent_after,
        beat=beat,
        alerts_on=alerts_on,
        send=send,
        check=check,
        ping=ping,
        care_token=care_token,
        get_json=get_json,
    )

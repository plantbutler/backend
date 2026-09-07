"""The wire: `k=v` in, `k=v` out, and the two shapes a report and a command are.

Every parser here refuses the whole message rather than keep the good half,
and every bound is half-open. The board has no clock and no JSON; what it
sends is a line of tokens, and what it cannot say correctly it must not be
able to say at all.
"""

import re
from typing import NamedTuple

from starlette.datastructures import QueryParams

from . import band, constants


class Report(NamedTuple):
    controller: str
    channels: dict[int, int]
    t: int | None  # board uptime, ms
    ack: int | None  # command id the board says it executed
    flow_ml: int | None  # what the flow meter counted while executing it
    float_ok: int | None  # reservoir float switch: 1 floats, 0 empty
    pos: str | None  # manifold position: 'ok' or 'unknown'
    err: str | None  # the board's last safety error token, when it sent one


class Command(NamedTuple):
    controller: str
    kind: str  # 'water' | 'stop'
    outlet: int | None
    ml: int | None
    cap_s: int | None


def _int_in(value: str, key: str, low: int, high: int) -> int:
    """One integer field, bounds half-open [low, high). ASCII digits only:
    bare int() would quietly repair Unicode digits, underscores and a leading
    `+` — values the board could never emit — into plausible numbers."""
    if not (value.isascii() and value.isdigit()):
        raise ValueError(f"{key}= is not an integer: {value!r}")
    n = int(value)
    if not low <= n < high:
        raise ValueError(f"{key}= out of range: {value}")
    return n


_CM = re.compile(r"\A\d{1,4}(?:\.\d{1,2})?\Z")


def _cm_in(value: str, key: str, high: float) -> float:
    """One measurement in centimetres, 0 exclusive to `high` inclusive.

    The same ASCII-only strictness as `_int_in`: bare float() takes "1e3",
    "inf", "nan", a Unicode digit and a leading `+`, and every one of those
    would reach log2() in the band engine. Zero is refused rather than read
    as unsaid — a 0 cm pot is a half-finished edit.
    """
    if not (value.isascii() and _CM.match(value)):
        raise ValueError(f"{key}= is not a measurement in cm: {value!r}")
    n = float(value)
    if not 0 < n <= high:
        raise ValueError(f"{key}= out of range: {value}")
    return n


def cm_from_text(text: str | None) -> float | None:
    """Centimetres out of free text like "14cm", "10" or "small", or None.

    The numbers carry over and the words do not: "small" would have to be
    invented into centimetres, and a wrong measurement moves the watering
    band where a missing one leaves it alone.
    """
    if not text:
        return None
    found = re.search(r"\d{1,4}(?:\.\d{1,2})?", text)
    if not found:
        return None
    n = float(found.group())
    return n if 0 < n <= 1000 else None


def parse_report(text: str) -> Report:
    """`k=v` tokens, whitespace-separated; line breaks are whitespace too.

    Strict about shape — a malformed token, a duplicate key, a non-integer or
    out-of-range value refuses the whole report — and silent about unknown
    keys, for the reasons the module docstring gives. ASCII digits only in a
    channel key: Unicode digits would alias onto ASCII channel numbers.
    """
    controller = None
    channels: dict[int, int] = {}
    t = ack = flow_ml = float_ok = pos = err = None
    for token in text.split():
        key, sep, value = token.partition("=")
        if not sep or not key:
            raise ValueError(f"not a k=v token: {token!r}")
        if key == "c":
            if controller is not None:
                raise ValueError("c= given twice")
            controller = _int_in(value, "c", 0, constants.MAX_CONTROLLER + 1)
        elif key == "t":
            if t is not None:
                raise ValueError("t= given twice")
            t = _int_in(value, "t", 0, 2**63)
        elif key == "ack":
            if ack is not None:
                raise ValueError("ack= given twice")
            ack = _int_in(value, "ack", 1, 2**63)  # command ids start at 1
        elif key == "flow_ml":
            if flow_ml is not None:
                raise ValueError("flow_ml= given twice")
            flow_ml = _int_in(value, "flow_ml", 0, constants.MAX_RAW)
        elif key == "float":
            if float_ok is not None:
                raise ValueError("float= given twice")
            float_ok = _int_in(value, "float", 0, 2)
        elif key == "pos":
            if pos is not None:
                raise ValueError("pos= given twice")
            if value not in ("ok", "unknown"):
                raise ValueError(f"pos= must be ok or unknown, got {value!r}")
            pos = value
        elif key == "err":
            if err is not None:
                raise ValueError("err= given twice")
            if not constants.ERR_TOKEN.match(value):
                raise ValueError(f"err= must be a short lowercase token, got {value!r}")
            err = value
        elif key.startswith("ch") and key[2:].isascii() and key[2:].isdigit():
            channel = int(key[2:])
            if channel > constants.MAX_CHANNEL:
                raise ValueError(f"channel index out of range: {key}")
            if channel in channels:
                raise ValueError(f"channel given twice: {key}")
            channels[channel] = _int_in(value, key, 0, constants.MAX_RAW)
    # `is None`, not falsiness: board 0 is a real board.
    if controller is None:
        raise ValueError("no c= in the report")
    if not channels:
        raise ValueError("no chN= in the report")
    if flow_ml is not None and ack is None:
        raise ValueError("flow_ml= without ack=")
    return Report(controller, channels, t, ack, flow_ml, float_ok, pos, err)


def cap_for(ml: int) -> int:
    """Seconds the pump may run for a dose: worst-case flow plus slack,
    bounded by MAX_CAP_S. The one owner of FLOW_FLOOR_ML_S — the rules and
    a manual command without cap_s= both size their cap here, so a bench
    retune happens in one place."""
    return min(constants.MAX_CAP_S, ml // constants.FLOW_FLOOR_ML_S + 5)


def parse_command(text: str) -> Command:
    """The `POST /command` body, same dialect and strictness as a report.

    `c=<controller>` plus either `water=<outlet> ml=<dose> [cap_s=<cap>]` or
    `stop=1`; a dose without cap_s= gets the rules' own cap. Unknown keys
    are ignored here too, so the app can grow fields before this service
    reads them.
    """
    controller = None
    outlet = ml = cap_s = None
    stop = False
    for token in text.split():
        key, sep, value = token.partition("=")
        if not sep or not key:
            raise ValueError(f"not a k=v token: {token!r}")
        if key == "c":
            if controller is not None:
                raise ValueError("c= given twice")
            controller = _int_in(value, "c", 0, constants.MAX_CONTROLLER + 1)
        elif key == "water":
            if outlet is not None:
                raise ValueError("water= given twice")
            outlet = _int_in(value, "water", 0, constants.MAX_CHANNEL + 1)
        elif key == "ml":
            if ml is not None:
                raise ValueError("ml= given twice")
            ml = _int_in(value, "ml", 1, constants.MAX_DOSE_ML + 1)
        elif key == "cap_s":
            if cap_s is not None:
                raise ValueError("cap_s= given twice")
            cap_s = _int_in(value, "cap_s", 1, constants.MAX_CAP_S + 1)
        elif key == "stop":
            if stop:
                raise ValueError("stop= given twice")
            if value != "1":
                raise ValueError(f"stop= must be 1, got {value!r}")
            stop = True
    if controller is None:  # board 0 is a real board
        raise ValueError("no c= in the command")
    if stop and not (outlet is None and ml is None and cap_s is None):
        raise ValueError("stop takes no dose")
    if stop:
        return Command(controller, "stop", None, None, None)
    if outlet is None:
        raise ValueError("neither water= nor stop=1")
    if ml is None:
        raise ValueError("water= needs ml=")
    return Command(
        controller, "water", outlet, ml, cap_for(ml) if cap_s is None else cap_s
    )


def parse_interval(text: str) -> tuple[str, int]:
    """The `POST /interval` body: `c=<controller> next=<seconds>`.

    `next=0` clears the override back to the BUTLER_NEXT_S default.
    """
    controller = None
    next_s = None
    for token in text.split():
        key, sep, value = token.partition("=")
        if not sep or not key:
            raise ValueError(f"not a k=v token: {token!r}")
        if key == "c":
            if controller is not None:
                raise ValueError("c= given twice")
            controller = _int_in(value, "c", 0, constants.MAX_CONTROLLER + 1)
        elif key == "next":
            if next_s is not None:
                raise ValueError("next= given twice")
            next_s = _int_in(value, "next", 0, constants.MAX_NEXT_S + 1)
            if 0 < next_s < constants.MIN_NEXT_S:
                raise ValueError(f"next= below {constants.MIN_NEXT_S}s: {value}")
    if controller is None:  # board 0 is a real board
        raise ValueError("no c= in the request")
    if next_s is None:
        raise ValueError("no next= in the request")
    return controller, next_s


def parse_controller(text: str) -> tuple[int, int]:
    """`c=<controller> retired=0|1`: the POST /controller body. Both fields,
    each once; unknown keys ignored like everywhere else on this wire."""
    controller = retired = None
    for token in text.split():
        key, sep, value = token.partition("=")
        if not sep or not key:
            raise ValueError(f"not a k=v token: {token!r}")
        if key == "c":
            if controller is not None:
                raise ValueError("c= given twice")
            controller = _int_in(value, "c", 0, constants.MAX_CONTROLLER + 1)
        elif key == "retired":
            if retired is not None:
                raise ValueError("retired= given twice")
            retired = _int_in(value, "retired", 0, 2)
    if controller is None:  # board 0 is a real board
        raise ValueError("no c= in the body")
    if retired is None:
        raise ValueError("no retired= in the body")
    return controller, retired


def parse_board(text: str) -> int:
    """A body that names a board and nothing else: `c=<controller>`."""
    controller = None
    for token in text.split():
        key, sep, value = token.partition("=")
        if not sep or not key:
            raise ValueError(f"not a k=v token: {token!r}")
        if key == "c":
            if controller is not None:
                raise ValueError("c= given twice")
            controller = _int_in(value, "c", 0, constants.MAX_CONTROLLER + 1)
    if controller is None:  # board 0 is a real board
        raise ValueError("no c= in the body")
    return controller


DOSES_MAX = 200


# A month back is the app's widest chart window, but the bucket cap is what
# actually bounds an answer: a month hourly is 744 points, a month at five
# minutes is refused. A week at one minute would be 10080 rows of JSON.
HISTORY_MAX_HOURS = 24 * 31
HISTORY_MAX_BUCKETS = 168 * 3600 // 300


def parse_doses(params: QueryParams) -> tuple[str | None, int, int | None, int]:
    """`GET /doses?pot=<pot id>&limit=<1..200>&before=<ts>&before_id=<id>`.

    No pot means the whole garden. `before`/`before_id` are the last row a
    caller already has: the page after it. Both together, because the list
    is ordered on (when it was handed out, id) and several doses can share
    a second — a cursor on the timestamp alone would skip or repeat them.
    The commands table is never pruned, so without this the older history
    would be permanently out of reach behind the newest `limit` rows.
    """

    def one(key: str, default: str | None = None) -> str | None:
        values = params.getlist(key)
        if len(values) > 1:
            raise ValueError(f"{key}= given twice")
        return values[0] if values else default

    pot = one("pot")
    if pot is not None and not pot:
        raise ValueError("pot= is empty")
    limit = _int_in(one("limit", "50") or "", "limit", 1, DOSES_MAX + 1)
    raw_before = one("before")
    before = None if raw_before is None else _int_in(raw_before, "before", 0, 1 << 42)
    raw_before_id = one("before_id")
    if raw_before_id is not None and before is None:
        raise ValueError("before_id= needs a before=")
    before_id = _int_in(raw_before_id or "0", "before_id", 0, 1 << 42)
    return pot, limit, before, before_id


def parse_history(params: QueryParams) -> tuple[str, int, int]:
    """`GET /history?pot=<pot id>&hours=<1..168>&bucket_s=<60..3600>`.

    Query parameters instead of a body because it is a read; the same
    ASCII-digit strictness and the same "given twice" refusal as every k=v
    field (a multidict would otherwise take the last value quietly), and
    the same plain-text refusal, so the app has one error dialect to show.
    """

    def one(key: str, default: str | None = None) -> str | None:
        values = params.getlist(key)
        if len(values) > 1:
            raise ValueError(f"{key}= given twice")
        return values[0] if values else default

    pot = one("pot")
    if not pot:
        raise ValueError("no pot= in the request")
    hours = _int_in(one("hours", "24") or "", "hours", 1, HISTORY_MAX_HOURS + 1)
    bucket_s = _int_in(one("bucket_s", "300") or "", "bucket_s", 60, 3601)
    if hours * 3600 // bucket_s > HISTORY_MAX_BUCKETS:
        raise ValueError(
            f"too many buckets: {hours} h at {bucket_s} s is over {HISTORY_MAX_BUCKETS}"
        )
    return pot, hours, bucket_s


POT_INT_FIELDS = {  # half-open bounds, like every other field
    "controller": (0, constants.MAX_CONTROLLER + 1),
    "channel": (0, constants.MAX_CHANNEL + 1),
    "outlet": (0, constants.MAX_CHANNEL + 1),
    "dry_raw": (0, constants.MAX_RAW),
    "wet_raw": (0, constants.MAX_RAW),
    "target_low_pct": (0, 101),
    "target_high_pct": (0, 101),
    "dose_ml": (1, constants.MAX_DOSE_ML + 1),
    "cooldown_h": (0, 8761),  # a year of cooldown is already a config error
    "daily_cap_ml": (0, 100_001),
}
POT_TEXT_FIELDS = ("species",)  # the one still typed rather than picked
POT_CM_FIELDS = {  # centimetres, and a plausible ceiling for each
    "pot_diameter_cm": 200.0,  # across the rim: a half-barrel and no further
    "plant_height_cm": 1000.0,  # a 10 m tree is not in a pot on the balcony
}
POT_MAP_FIELDS = ("controller", "channel", "outlet")  # pot_mappings, not pots
POT_MODES = ("manual", "learning", "auto")
# What a pot IS, not what it may do. A closed set, shaped so a third word
# (paused-but-wired, say) is one entry here plus one label in the app.
POT_STATUSES = ("alive", "graveyard")


def parse_pot(text: str) -> dict:
    """The `POST /pot` body: which pot, plus whatever fields to set.

    An `id=` is an EDIT of that pot — name included, so renaming is an
    ordinary field edit and no history is orphaned by it. A bare `name=`
    is a create, and mints an id.

    A partial upsert either way — only the keys given change, so
    recalibration is `id=pot-3f9a21 dry_raw=13000 wet_raw=4200` and
    nothing else moves. Values are single k=v tokens, so multi-word text
    uses underscores. Unknown keys are ignored, for the same reason as
    everywhere else.
    """
    fields: dict = {}
    known = {
        "id",
        "name",
        "mode",
        "plant_type",
        "soil",
        "status",
        *POT_TEXT_FIELDS,
        *POT_CM_FIELDS,
        *POT_INT_FIELDS,
    }
    for token in text.split():
        key, sep, value = token.partition("=")
        if not sep or not key:
            raise ValueError(f"not a k=v token: {token!r}")
        if key not in known:
            continue
        if key in fields:
            raise ValueError(f"{key}= given twice")
        if not value:
            raise ValueError(f"{key}= is empty")
        if key in POT_INT_FIELDS:
            low, high = POT_INT_FIELDS[key]
            fields[key] = _int_in(value, key, low, high)
        elif key in POT_CM_FIELDS:
            fields[key] = _cm_in(value, key, POT_CM_FIELDS[key])
        elif key == "mode":
            if value not in POT_MODES:
                raise ValueError(f"mode= must be one of {'|'.join(POT_MODES)}")
            fields[key] = value
        elif key == "plant_type":
            # Closed on the way in, tolerant on the way out: a value from
            # outside the set still reads and simply matches no band. The
            # refusal here is what stops a free-text "basil" looking saved
            # while it picks no watering band at all.
            if value not in band.PLANT_KINDS:
                raise ValueError(
                    f"plant_type= must be one of {'|'.join(band.PLANT_KINDS)}"
                )
            fields[key] = value
        elif key == "soil":
            # Closed in and tolerant out, as plant_type is: a value from
            # outside the set reads fine and matches no shift.
            if value not in band.SOIL_SHIFTS:
                raise ValueError(f"soil= must be one of {'|'.join(band.SOIL_SHIFTS)}")
            fields[key] = value
        elif key == "status":
            if value not in POT_STATUSES:
                raise ValueError(f"status= must be one of {'|'.join(POT_STATUSES)}")
            fields[key] = value
        else:
            fields[key] = value
    if "id" not in fields and "name" not in fields:
        raise ValueError("no id= or name= in the request")
    if fields.get("status") == "graveyard" and any(k in fields for k in POT_MAP_FIELDS):
        # Graveyarding is what UNWIRES a pot, so a body that does both at
        # once is asking for two opposite things. Asked of the request, not
        # of the merged row: graveyarding a pot that is wired right now is
        # the whole point and must go through.
        raise ValueError("a graveyard pot holds no wiring: send status=graveyard alone")
    return fields


def parse_approve(text: str) -> int:
    """The `POST /approve` body: `cmd=<id>`."""
    cmd_id = None
    for token in text.split():
        key, sep, value = token.partition("=")
        if not sep or not key:
            raise ValueError(f"not a k=v token: {token!r}")
        if key == "cmd":
            if cmd_id is not None:
                raise ValueError("cmd= given twice")
            cmd_id = _int_in(value, "cmd", 1, 2**63)
    if cmd_id is None:
        raise ValueError("no cmd= in the request")
    return cmd_id


def parse_verdict(text: str) -> tuple[int, str]:
    """The `POST /verdict` body: `cmd=<id> verdict=ok|too_much|too_little`."""
    cmd_id = None
    verdict = None
    for token in text.split():
        key, sep, value = token.partition("=")
        if not sep or not key:
            raise ValueError(f"not a k=v token: {token!r}")
        if key == "cmd":
            if cmd_id is not None:
                raise ValueError("cmd= given twice")
            cmd_id = _int_in(value, "cmd", 1, 2**63)
        elif key == "verdict":
            if verdict is not None:
                raise ValueError("verdict= given twice")
            if value not in constants.VERDICT_VALUES:
                raise ValueError(
                    f"verdict= must be one of {'|'.join(constants.VERDICT_VALUES)}"
                )
            verdict = value
    if cmd_id is None:
        raise ValueError("no cmd= in the request")
    if verdict is None:
        raise ValueError("no verdict= in the request")
    return cmd_id, verdict


ADVICE_KINDS = ("target",)


def parse_advice(text: str) -> tuple[str, str]:
    """The `POST /advice` body: `pot=<id> kind=target dismiss=1`.

    `dismiss=1` is spelled out rather than implied by the endpoint, so that
    an accept can never be a typo away: there is no accept here at all, and
    a body that asks for one is refused instead of quietly dismissing.
    """
    fields: dict = {}
    for token in text.split():
        key, sep, value = token.partition("=")
        if not sep or not key:
            raise ValueError(f"not a k=v token: {token!r}")
        if key in fields:
            raise ValueError(f"{key}= given twice")
        fields[key] = value
    pot_id = fields.get("pot")
    if not pot_id:
        raise ValueError("no pot= in the request")
    kind = fields.get("kind", "target")
    if kind not in ADVICE_KINDS:
        raise ValueError(f"kind= must be one of {'|'.join(ADVICE_KINDS)}")
    if fields.get("dismiss") != "1":
        raise ValueError("dismiss=1 is the only thing this endpoint does")
    return pot_id, kind


def parse_photo(params: QueryParams) -> tuple[str, int | None, int | None]:
    """`POST /photo?pot=<id>&w=&h=`, with the JPEG as the body.

    The one route here whose payload is not k=v, because it is bytes; the
    metadata rides the query string instead of a multipart envelope, which
    would be a dependency and a parser for one field and a file.

    `w`/`h` are what the phone says it downscaled to. They are a hint for
    laying out the strip before the bytes arrive, nothing more — no one
    here opens the JPEG to check, and nothing is decided from them.
    """

    def one(key: str) -> str | None:
        values = params.getlist(key)
        if len(values) > 1:
            raise ValueError(f"{key}= given twice")
        return values[0] if values else None

    pot = (one("pot") or "").strip()
    if not pot:
        raise ValueError("no pot= in the request")
    if not constants.SAFE_ID.fullmatch(pot):
        raise ValueError(f"not a pot id: {pot!r}")

    def edge(key: str) -> int | None:
        raw = one(key)
        return None if raw is None else _int_in(raw, key, 1, constants.MAX_PHOTO_EDGE)

    return pot, edge("w"), edge("h")


def parse_photos(params: QueryParams) -> tuple[str, int]:
    """`GET /photos?pot=<id>&limit=<1..500>`: one pot's strip, newest first.

    A pot is required, unlike /doses: a photograph belongs to a plant's own
    growth history, and a garden-wide roll of them is a gallery.
    """

    def one(key: str, default: str | None = None) -> str | None:
        values = params.getlist(key)
        if len(values) > 1:
            raise ValueError(f"{key}= given twice")
        return values[0] if values else default

    pot = (one("pot") or "").strip()
    if not pot:
        raise ValueError("no pot= in the request")
    if not constants.SAFE_ID.fullmatch(pot):
        raise ValueError(f"not a pot id: {pot!r}")
    # +1 like every other bound here: _int_in's top is exclusive, and the
    # named max is meant to be a limit somebody can actually ask for.
    return pot, _int_in(
        one("limit", str(constants.PHOTO_LIMIT)) or "",
        "limit",
        1,
        constants.MAX_PHOTO_LIMIT + 1,
    )


def parse_photo_delete(text: str) -> str:
    """The `POST /photo/delete` body: `photo=<id>`.

    Its own route rather than a `delete=1` field on /photo, because /photo
    carries a picture and this one must never be reachable by an upload
    that lost its body.
    """
    fields: dict = {}
    for token in text.split():
        key, sep, value = token.partition("=")
        if not sep or not key:
            raise ValueError(f"not a k=v token: {token!r}")
        if key in fields:
            raise ValueError(f"{key}= given twice")
        fields[key] = value
    photo_id = fields.get("photo")
    if not photo_id:
        raise ValueError("no photo= in the request")
    if not constants.SAFE_ID.fullmatch(photo_id):
        raise ValueError(f"not a photo id: {photo_id!r}")
    return photo_id


def parse_pot_delete(text: str) -> str:
    """The `POST /pot/delete` body: `id=<pot id>`.

    Its own route rather than a field on /pot, for the same reason and a
    louder one: a save that lost its body must never become an erasure.
    SAFE_ID is not decoration here — the delete turns this id into the
    directory `photos/<pot id>/` and removes it.
    """
    fields: dict = {}
    for token in text.split():
        key, sep, value = token.partition("=")
        if not sep or not key:
            raise ValueError(f"not a k=v token: {token!r}")
        if key in fields:
            raise ValueError(f"{key}= given twice")
        fields[key] = value
    pot_id = fields.get("id")
    if not pot_id:
        raise ValueError("no id= in the request")
    if not constants.SAFE_ID.fullmatch(pot_id):
        raise ValueError(f"not a pot id: {pot_id!r}")
    return pot_id


def parse_quiet(text: str) -> tuple[int, int]:
    """BUTLER_QUIET, `HH-HH` in the server's local time; `0-0` disables.

    The container runs UTC unless TZ is set — set TZ in the deployment or
    the quiet window is quiet somewhere else.
    """
    start, sep, end = text.partition("-")
    ok = sep and start.isascii() and start.isdigit() and end.isascii() and end.isdigit()
    if not ok:
        raise ValueError(f"BUTLER_QUIET must be HH-HH, got {text!r}")
    s, e = int(start), int(end)
    if not (0 <= s <= 23 and 0 <= e <= 23):
        raise ValueError(f"BUTLER_QUIET hours out of range: {text}")
    return s, e


def in_quiet(hour: int, start: int, end: int) -> bool:
    """Whether `hour` falls in the quiet window; start == end means never."""
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end

"""The numbers the whole butler agrees on.

Every one of them was decided once, and the comments say by what. A bare
integer here is a promise to the firmware, to the pump, or to the person the
alert wakes up.
"""

import re


BODY_CAP = 4096  # a full 15-channel report is ~200 bytes; 4 KB is generous
# The phone caps the long edge before uploading, which puts a JPEG at
# 300-500 KB; this is room for a bad guess, not a size anyone should reach.
PHOTO_CAP = 3 * 1024 * 1024
JPEG_HEAD = b"\xff\xd8\xff"  # every JPEG starts SOI + a marker
PHOTO_LIMIT = 100  # the strip's default page
MAX_PHOTO_LIMIT = 500
MAX_PHOTO_EDGE = 8192  # w=/h= are what the phone says it downscaled to
# Ids are four random bytes; retrying keeps a collision from being a 500.
PHOTO_ID_TRIES = 4
# Every id that becomes part of a filesystem path passes this first: it is
# what stops a `pot=../../etc` writing outside the photo store. Ids here are
# minted, so nothing legitimate is turned away.
SAFE_ID = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")
# err= is the board's last safety error: a short lowercase token, digits
# included (contra, resetmid, range, heap, i2c). It is sticky, so a token
# refused here makes every later report a 400 until the board reboots.
# Bounded so a stray value cannot become unbounded TEXT on status.
ERR_TOKEN = re.compile(r"\A[a-z0-9_]{1,16}\Z")
RETRY_WINDOW_S = 300  # how long an identical (controller, t) counts as a retry
MAX_CHANNEL = 255
# The board's own number on the wire. Board 0 is a real board and is falsy,
# so every check on a controller is `is None`, never truthiness.
MAX_CONTROLLER = 255
# The board's three latches, sent as channels: 1 in every report while each
# stands, absent meaning 0 (an older board, never latched). The contra is
# "float said full, meter saw nothing"; it lives in the board's .noinit,
# which a power cycle erases, so the durable half is here. The flap is three
# consecutive float refusals, which force float= to 0 until a dose is
# granted — it says WHY the word is 0, and a refill tap answers it for one
# dose. The dry latch is a board held dry by a reset mid-dose or by `dry on`
# at the console, and only `dry off` releases it.
CONTRA_CHANNEL = 207
FLAP_CHANNEL = 210
DRY_CHANNEL = 211
# The two levels plus the reset's edge (`err=` turning to resetmid), which is
# consulted beside them: a `dry off` typed before the first post-reset report
# leaves that edge as the reset's only trace.
LATCH_TEXT = {
    "contra": "the float said full and the meter saw nothing",
    "dry": "the board is held dry: a reset with the pump running, or dry on at the console",
    "resetmid": "it reset with the pump running",
}
# What the human types on the board's console. `clear contra` lifts the
# contradiction latch and nothing else; a board held dry is let go only by
# `dry off`. The 409 and the latch page both spell the step out of this one
# map, so nobody is sent to type the wrong word.
LATCH_STEP = {
    "contra": "type clear contra on the board",
    "dry": "type dry off on the board",
    "resetmid": "type dry off on the board",
}
MAX_RAW = 2**31  # 14-bit ADC today; headroom without letting 2**63 near sqlite
# The board's own PB_DOSE_RIG_MAX_ML; the two move together. Above it the
# firmware refuses with err=range and acks flow_ml=0, so the pot is charged
# nothing, cooled down, paged and never watered — a loop that refusing here,
# at /command and at pot save, keeps unreachable.
MAX_DOSE_ML = 250
MAX_CAP_S = 60  # the firmware enforces its own cap; this bounds what we ask
MIN_NEXT_S, MAX_NEXT_S = 5, 3600  # the interval knob's sane range
RULES_WINDOW = 5  # median of this many readings is the whole of the smoothing
PROPOSAL_TTL_S = 7200  # a proposal nobody approved in 2 h expires
DEFAULT_COOLDOWN_H = 6  # when a pot does not set its own; 0 disables
DEFAULT_DAILY_CAP_DOSES = 3  # a NULL daily_cap_ml means this many doses
FLOW_FLOOR_ML_S = 20  # worst-case pump flow, sizes cap_s; bench-rig-tunable
VERDICT_VALUES = ("ok", "too_much", "too_little")
ALERT_TICK_S = 60  # the alert ticker's beat; a create_app parameter in tests
SILENT_AFTER_S = 600  # BUTLER_SILENT_S default; the floor is 3x the interval
PERSIST_S = 180  # a status must hold this long before it raises or clears
REALERT_FLOOR_S = 3600  # a cleared condition sounds again at most hourly
SOAK_S = 1800  # water needs this long to reach the sensor before judging
MIN_RISE_PCT = 5  # a dose that raised moisture less than this did not work
DOSE_LOOKBACK_S = 86400  # doses older than a day are history, not news
PROPOSAL_NUDGE_S = 86400  # one proposal nudge per hose per day
UP_AFTER_S = 600  # the one "butler is up" probe, once uptime clears this
NTFY_TIMEOUT_S = 10
FLAP_WINDOW_S = 600  # two bad float/pos sightings this close together raise
RESUME_GRACE_S = 600  # a restart shorter than this keeps the observation window
UP_PROBE_FLOOR_S = 86400  # the up-probe fires at most daily, across restarts
# The tank's size is measured, not configured: one sample is the millilitres
# the meter counted between a refill tap and the float going empty, and the
# size is the median of the last TANK_MEDIAN_OF. The float is then judged
# against that volume and never against a clock — water past the size plus
# TANK_TOLERANCE_PCT with the float still at 1 is a float presumed stuck.
TANK_SAMPLES_TO_ARM = 2
TANK_MEDIAN_OF = 5
TANK_TOLERANCE_PCT = 10
TANK_DRIFT_PCT = 25

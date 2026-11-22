"""Decision logic of the generalized sticky balancer (no gRPC needed)."""
import datetime
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import tunnel_lib as tl  # noqa: E402
from scripts.sticky_balancer import FAIL_THRESHOLD, STABILITY_TICKS, Lane  # noqa: E402

NOW = datetime.datetime(2026, 11, 23, 12, 0)
ORDER = ["nl1", "nl2", "de1", "ru1"]
CC = {"nl1": "NL", "nl2": "NL", "de1": "DE", "ru1": "RU"}


def st(**delays):
    return {t: {"alive": d > 0, "delay": d} for t, d in delays.items()}


def cfg(**kw):
    c = tl.default_lane()
    c.update(kw)
    return c


def test_initial_selection_respects_filter():
    lane = Lane("global", "proxy-auto")
    c = cfg(filter={"mode": "except", "countries": ["RU"]})
    res = lane.decide(c, st(nl1=300, nl2=200, de1=250, ru1=50), ORDER, CC, "", NOW)
    assert res[0] == "nl2" and res[1] == "initial_selection"


def test_policy_change_moves_off_disallowed_node():
    lane = Lane("global", "proxy-auto")
    lane.active_node = "ru1"
    c = cfg(filter={"mode": "only", "countries": ["DE"]})
    res = lane.decide(c, st(nl1=300, nl2=200, de1=250, ru1=50), ORDER, CC, "ru1", NOW)
    assert res[0] == "de1" and res[1].startswith("policy_filter")


def test_no_allowed_alive_falls_back_softly():
    lane = Lane("global", "proxy-auto")
    lane.active_node = "nl1"
    c = cfg(filter={"mode": "only", "countries": ["DE"]})
    statuses = st(nl1=300, nl2=200, de1=0, ru1=50)
    assert lane.decide(c, statuses, ORDER, CC, "nl1", NOW) is None
    assert lane.violation


def test_manual_holds_then_fails_over_and_returns():
    lane = Lane("global", "proxy-auto")
    c = cfg(mode="manual", node="nl1")
    alive = st(nl1=900, nl2=100, de1=100, ru1=100)
    res = lane.decide(c, alive, ORDER, CC, "nl2", NOW)
    assert res == ("nl1", "manual_pin")
    lane.switched(*res)
    # slower than others but pinned: no hysteresis switch
    for _ in range(STABILITY_TICKS + 2):
        assert lane.decide(c, alive, ORDER, CC, "nl1", NOW) is None
    dead = st(nl1=0, nl2=100, de1=120, ru1=100)
    for _ in range(FAIL_THRESHOLD - 1):
        assert lane.decide(c, dead, ORDER, CC, "nl1", NOW) is None
    res = lane.decide(c, dead, ORDER, CC, "nl1", NOW)
    assert res[0] in ("nl2", "ru1") and res[1].startswith("manual_failover")
    lane.switched(*res)
    # stays on the failover node while the pin is dead (no flapping back)
    assert lane.decide(c, dead, ORDER, CC, lane.active_node, NOW) is None
    # pin comes back -> return to it
    assert lane.decide(c, alive, ORDER, CC, lane.active_node, NOW) == ("nl1", "manual_pin")


def test_forced_rotation_next_strategy():
    lane = Lane("global", "proxy-auto")
    lane.active_node = "nl1"
    c = cfg(rotation={"schedule": "daily", "strategy": "next"}, filter={"mode": "except", "countries": ["RU"]})
    statuses = st(nl1=100, nl2=100, de1=100, ru1=100)
    assert lane.decide(c, statuses, ORDER, CC, "nl1", NOW) is None
    res = lane.decide(c, statuses, ORDER, CC, "nl1", NOW, rotate_request="2026-11-23T12:00:00")
    assert res[0] == "nl2" and res[1].startswith("manual_rotation/next")
    lane.switched(*res)
    # same request is not handled twice
    assert lane.decide(c, statuses, ORDER, CC, "nl2", NOW, rotate_request="2026-11-23T12:00:00") is None


def test_interval_rotation_is_scheduled_and_fires():
    lane = Lane("client:192.168.1.5", "client-192_168_1_5")
    lane.active_node = "nl1"
    c = cfg(rotation={"schedule": "1h", "strategy": "next"})
    statuses = st(nl1=100, nl2=100, de1=100, ru1=100)
    assert lane.decide(c, statuses, ORDER, CC, "nl1", NOW) is None
    assert lane.next_rotation == NOW + datetime.timedelta(hours=1)
    res = lane.decide(c, statuses, ORDER, CC, "nl1", NOW + datetime.timedelta(hours=1, seconds=1))
    assert res[0] == "nl2" and res[1].startswith("scheduled_rotation")


def test_hysteresis_still_works_in_auto():
    lane = Lane("global", "proxy-auto")
    lane.active_node = "nl1"
    c = cfg()
    statuses = st(nl1=600, nl2=100, de1=500, ru1=0)
    results = [lane.decide(c, statuses, ORDER, CC, "nl1", NOW) for _ in range(STABILITY_TICKS)]
    assert results[:-1] == [None] * (STABILITY_TICKS - 1)
    assert results[-1][0] == "nl2" and results[-1][1].startswith("hysteresis_switch")

import datetime
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest
from scripts.sticky_balancer import compute_next_rotation, ROTATION_HOUR, ROTATION_WINDOW_MIN


def test_compute_next_rotation_future_today():
    # If currently 04:00 AM, next rotation should be today around 06:00 AM
    now = datetime.datetime(2026, 11, 23, 4, 0, 0)
    nxt = compute_next_rotation(now)
    assert nxt.day == 23
    assert nxt > now
    min_window = datetime.datetime(2026, 11, 23, ROTATION_HOUR, 0, 0) - datetime.timedelta(minutes=ROTATION_WINDOW_MIN)
    max_window = datetime.datetime(2026, 11, 23, ROTATION_HOUR, 0, 0) + datetime.timedelta(minutes=ROTATION_WINDOW_MIN)
    assert min_window <= nxt <= max_window


def test_compute_next_rotation_past_today():
    # If currently 12:00 PM (noon), next rotation should be tomorrow around 06:00 AM
    now = datetime.datetime(2026, 11, 23, 12, 0, 0)
    nxt = compute_next_rotation(now)
    assert nxt.day == 24
    assert nxt > now
    min_window = datetime.datetime(2026, 11, 24, ROTATION_HOUR, 0, 0) - datetime.timedelta(minutes=ROTATION_WINDOW_MIN)
    max_window = datetime.datetime(2026, 11, 24, ROTATION_HOUR, 0, 0) + datetime.timedelta(minutes=ROTATION_WINDOW_MIN)
    assert min_window <= nxt <= max_window

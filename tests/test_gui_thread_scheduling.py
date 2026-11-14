"""Application scheduling must favor GUI progress without undoing user tuning."""
import sys

import pytest

from app import configure_gui_thread_scheduling


@pytest.mark.parametrize('initial', [.005, .0005])
def test_startup_bounds_thread_switch_interval_and_keeps_shorter_setting(initial):
    previous = sys.getswitchinterval()
    try:
        sys.setswitchinterval(initial)
        configure_gui_thread_scheduling()
        assert sys.getswitchinterval() == pytest.approx(min(initial, .001), abs=.000002)
    finally:
        sys.setswitchinterval(previous)

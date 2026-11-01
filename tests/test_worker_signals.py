import asyncio
import signal

import pytest

from arena.worker import install_stop_signals


async def test_first_signal_sets_stop_and_restores_previous_handlers():
    before = signal.getsignal(signal.SIGTERM)
    stop = asyncio.Event()
    restore = install_stop_signals(stop)
    try:
        signal.raise_signal(signal.SIGTERM)
        await asyncio.wait_for(stop.wait(), 2)
        # A second signal must get the previous (default) behaviour, not be swallowed.
        assert signal.getsignal(signal.SIGTERM) == before
    finally:
        restore()
    assert signal.getsignal(signal.SIGTERM) == before


@pytest.mark.skipif(not hasattr(signal, "SIGBREAK"), reason="Windows only")
async def test_ctrl_break_stops_like_sigterm_on_windows():
    stop = asyncio.Event()
    before = signal.getsignal(signal.SIGBREAK)
    restore = install_stop_signals(stop)
    try:
        signal.raise_signal(signal.SIGBREAK)
        await asyncio.wait_for(stop.wait(), 1)
    finally:
        restore()
    assert signal.getsignal(signal.SIGBREAK) == before

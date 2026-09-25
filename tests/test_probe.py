import pytest

from sdwan.probe import ProbeRecord, summarize_window

MS = 1_000_000  # nanoseconds per millisecond


def rec(seq, send_ms, rtt_ms=None):
    send_ns = send_ms * MS
    recv_ns = None if rtt_ms is None else send_ns + int(rtt_ms * MS)
    return ProbeRecord(seq=seq, send_ns=send_ns, recv_ns=recv_ns)


def test_all_received_has_zero_loss_and_mean_rtt():
    records = [rec(1, 100, 10), rec(2, 350, 12), rec(3, 600, 11), rec(4, 850, 15)]
    rtt, _, loss = summarize_window(records, 0, 1000 * MS)
    assert loss == 0.0
    assert rtt == pytest.approx(12.0)


def test_one_of_four_lost_is_25_percent():
    records = [rec(1, 100, 10), rec(2, 350), rec(3, 600, 11), rec(4, 850, 15)]
    _, _, loss = summarize_window(records, 0, 1000 * MS)
    assert loss == 25.0


def test_none_received_reports_full_loss_and_no_rtt():
    records = [rec(1, 100), rec(2, 350)]
    assert summarize_window(records, 0, 1000 * MS) == (None, None, 100.0)


def test_jitter_is_mean_absolute_difference_of_consecutive_rtts():
    records = [rec(1, 100, 10), rec(2, 350, 12), rec(3, 600, 11)]
    _, jitter, _ = summarize_window(records, 0, 1000 * MS)
    assert jitter == pytest.approx(1.5)


def test_single_reply_has_zero_jitter():
    _, jitter, _ = summarize_window([rec(1, 100, 10), rec(2, 350)], 0, 1000 * MS)
    assert jitter == 0.0


def test_window_excludes_start_and_includes_end():
    # seq 1 sits exactly on start (excluded), seq 3 exactly on end (included),
    # seq 4 is after the end. Only the lost ones outside the window would
    # change the loss figure, so loss == 0 proves the boundaries.
    records = [rec(1, 0), rec(2, 500, 10), rec(3, 1000, 10), rec(4, 1500)]
    assert summarize_window(records, 0, 1000 * MS) == (10.0, 0.0, 0.0)


def test_empty_window_returns_none():
    assert summarize_window([rec(1, 5000, 10)], 0, 1000 * MS) is None

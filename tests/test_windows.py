import time

from momentum.data.windows import TimeSeriesBuffer


def test_value_n_seconds_ago():
    buf = TimeSeriesBuffer(max_age_s=60)
    t0 = time.time()
    buf.append(t0 - 20, 100.0)
    buf.append(t0 - 10, 105.0)
    buf.append(t0, 110.0)

    assert buf.value_n_seconds_ago(t0, 10) == 105.0
    assert buf.value_n_seconds_ago(t0, 0) == 110.0


def test_sum_and_avg_since():
    buf = TimeSeriesBuffer(max_age_s=60)
    t0 = time.time()
    for i in range(5):
        buf.append(t0 - i, 1.0)

    assert buf.sum_since(t0, 3) == 3.0 or buf.sum_since(t0, 3) >= 3.0
    assert buf.avg_since(t0, 10) == 1.0


def test_trims_old_points():
    buf = TimeSeriesBuffer(max_age_s=5)
    t0 = time.time()
    buf.append(t0 - 10, 1.0)
    buf.append(t0, 2.0)
    assert len(buf) == 1
    assert buf.latest() == 2.0

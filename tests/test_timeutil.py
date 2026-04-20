from src.timeutil import cron_hm_before_deadline


def test_cron_hm_before_deadline_basic() -> None:
    assert cron_hm_before_deadline("11:00", 20) == (10, 40)


def test_cron_hm_before_deadline_wraps_midnight() -> None:
    assert cron_hm_before_deadline("00:10", 20) == (23, 50)


def test_cron_hm_before_deadline_ten_minutes() -> None:
    assert cron_hm_before_deadline("11:00", 10) == (10, 50)

from datetime import datetime

import pytest

from bot.services.dates import parse_due

SAT = datetime(2026, 11, 7, 10, 0)  # суббота
FRI_MORNING = datetime(2026, 11, 13, 10, 0)  # пятница до 16:00
FRI_EVENING = datetime(2026, 11, 13, 17, 0)  # пятница после 16:00


@pytest.mark.parametrize(
    "text, expected",
    [
        ("пятница", datetime(2026, 11, 13, 18, 0)),
        ("до пятницы", datetime(2026, 11, 13, 18, 0)),
        ("в пятницу", datetime(2026, 11, 13, 18, 0)),
        ("пт", datetime(2026, 11, 13, 18, 0)),
        ("Понедельник", datetime(2026, 11, 9, 18, 0)),
        ("к среде", datetime(2026, 11, 11, 18, 0)),
        ("воскресенье", datetime(2026, 11, 8, 18, 0)),
        ("завтра", datetime(2026, 11, 8, 18, 0)),
        ("послезавтра", datetime(2026, 11, 9, 18, 0)),
        ("15.11.2026", datetime(2026, 11, 15, 18, 0)),
        ("15.11.26", datetime(2026, 11, 15, 18, 0)),
        ("20.11", datetime(2026, 11, 20, 18, 0)),
        ("01.03", datetime(2027, 3, 1, 18, 0)),  # без года и уже прошло → следующий год
        ("пятница 12:30", datetime(2026, 11, 13, 12, 30)),
        ("20.11 в 9:00", datetime(2026, 11, 20, 9, 0)),
    ],
)
def test_regular_due(text, expected):
    result = parse_due(text, SAT)
    assert result is not None
    assert result.due == expected
    assert result.alternative is None


def test_same_day_before_16_is_16_00():
    result = parse_due("пятница", FRI_MORNING)
    assert result.due == datetime(2026, 11, 13, 16, 0)
    assert result.alternative is None


def test_same_day_after_16_offers_next_week():
    result = parse_due("пятница", FRI_EVENING)
    assert result.due == datetime(2026, 11, 13, 16, 0)
    assert result.alternative == datetime(2026, 11, 20, 18, 0)


def test_today_after_16_offers_tomorrow():
    result = parse_due("сегодня", FRI_EVENING)
    assert result.alternative == datetime(2026, 11, 14, 18, 0)


def test_today_explicit_date_is_16_00():
    assert parse_due("07.11.2026", SAT).due == datetime(2026, 11, 7, 16, 0)


@pytest.mark.parametrize("text", ["", "когда-нибудь", "31.02.2026", "пятница 25:00", "пятница или суббота"])
def test_unparseable(text):
    assert parse_due(text, SAT) is None

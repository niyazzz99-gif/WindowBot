import asyncio
from datetime import date, datetime, timedelta

import httpx
import pytest

from bot.models import Task, WorkType
from bot.services.formatting import format_weather
from bot.services.weather import (
    DailyForecast,
    OpenMeteoClient,
    Thresholds,
    WeatherService,
    check_thresholds,
)

TODAY = date(2026, 11, 7)


def daily_payload(days: int = 11, tmin=-1.0, wind=3.0, prec=0.0, overrides=None) -> dict:
    """Ответ Open-Meteo; overrides = {индекс дня: (tmin, wind, prec)}."""
    rows = [(tmin, wind, prec)] * days
    for i, v in (overrides or {}).items():
        rows[i] = v
    return {"daily": {
        "time": [(TODAY + timedelta(days=i)).isoformat() for i in range(days)],
        "temperature_2m_min": [r[0] for r in rows],
        "windspeed_10m_max": [r[1] for r in rows],
        "precipitation_sum": [r[2] for r in rows],
    }}


class Api:
    """Фейковый Open-Meteo на httpx.MockTransport."""

    def __init__(self, payload=None, status=200) -> None:
        self.payload, self.status, self.requests = payload or daily_payload(), status, []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json=self.payload)

    def service(self, thresholds=Thresholds()) -> WeatherService:
        http = httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        return WeatherService(OpenMeteoClient(55.75, 37.61, http), thresholds)


def task(work_type=WorkType.MOUNTING, days_ahead=2) -> Task:
    due = datetime.combine(TODAY + timedelta(days=days_ahead), datetime.min.time()).replace(hour=18)
    return Task(work_type=work_type, order_no="1", due=due, assignee="Иван", chat_id=1)


# --- пороги ---

@pytest.mark.parametrize("forecast, expected", [
    (DailyForecast(TODAY, -15, 3, 0), ["холод"]),
    (DailyForecast(TODAY, -10, 3, 0), []),  # ровно порог — не предупреждаем
    (DailyForecast(TODAY, 0, 12.4, 0), ["ветер"]),
    (DailyForecast(TODAY, 0, 10, 0), []),
    (DailyForecast(TODAY, 0, 3, 0.1), ["осадки"]),  # любые ненулевые осадки
    (DailyForecast(TODAY, -20, 15, 5), ["холод", "ветер", "осадки"]),
    (DailyForecast(TODAY, None, None, None), []),  # нет данных — не выдумываем
])
def test_check_thresholds(forecast, expected):
    reasons = check_thresholds(forecast, Thresholds(temp_min=-10, wind_max=10))
    assert [r.split()[0] for r in reasons] == expected


@pytest.mark.parametrize("v, s", [(-18.4, "-18 °C"), (3, "+3 °C"), (-0.2, "0 °C")])
def test_fmt_temp(v, s):
    from bot.services.weather import fmt_temp

    assert fmt_temp(v) == s


def test_custom_thresholds():
    assert check_thresholds(DailyForecast(TODAY, -5, 3, 0), Thresholds(temp_min=-3)) != []


# --- клиент и сервис ---

def test_request_params():
    api = Api()
    asyncio.run(api.service().check_task(task(), TODAY))
    params = api.requests[0].url.params
    assert params["daily"] == "temperature_2m_min,windspeed_10m_max,precipitation_sum"
    assert params["wind_speed_unit"] == "ms"
    assert params["start_date"] == "2026-11-07" and params["end_date"] == "2026-11-17"


def test_cold_mounting_warns():
    api = Api(daily_payload(overrides={2: (-18.0, 4.0, 0.0)}))
    check = asyncio.run(api.service().check_task(task(days_ahead=2), TODAY))
    assert check.has_warnings
    text = format_weather(check)
    assert "⚠️" in text and "-18 °C" in text and "пн 09.11" in text


def test_good_weather_no_warning():
    check = asyncio.run(Api().service().check_task(task(), TODAY))
    assert not check.has_warnings
    assert "без ограничений" in format_weather(check)


def test_dismantling_is_checked_painting_is_not():
    svc = Api(daily_payload(prec=2.0)).service()
    assert asyncio.run(svc.check_task(task(WorkType.DISMANTLING), TODAY)).has_warnings
    assert asyncio.run(svc.check_task(task(WorkType.PAINTING), TODAY)) is None
    assert asyncio.run(svc.check_task(task(WorkType.MAINTENANCE), TODAY)) is None


@pytest.mark.parametrize("days_ahead, checked", [(0, True), (10, True), (11, False), (-1, False)])
def test_horizon_10_days(days_ahead, checked):
    check = asyncio.run(Api().service().check_task(task(days_ahead=days_ahead), TODAY))
    assert (check is not None) is checked


def test_api_error_gives_unavailable_message():
    check = asyncio.run(Api(status=503).service().check_task(task(), TODAY))
    assert check.error and not check.has_warnings
    assert "недоступен" in format_weather(check)


def test_broken_payload_is_error_not_crash():
    check = asyncio.run(Api(payload={"oops": 1}).service().check_task(task(), TODAY))
    assert check.error


def test_one_request_for_many_checks():
    api = Api()
    svc = api.service()

    async def scenario():
        for d in range(5):
            await svc.check_task(task(days_ahead=d), TODAY)

    asyncio.run(scenario())
    assert len(api.requests) == 1  # кэш на весь горизонт

"""Прогноз погоды Open-Meteo (без ключа) и проверка порогов для уличных работ."""

import logging
import time
from dataclasses import dataclass, field
from datetime import date, timedelta

import httpx

from bot.models import Task

log = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
DAILY_FIELDS = ("temperature_2m_min", "windspeed_10m_max", "precipitation_sum")
# Дальше 10 дней прогноз малоточен — не проверяем
MAX_DAYS_AHEAD = 10
CACHE_TTL_S = 30 * 60


class WeatherError(Exception):
    """Open-Meteo недоступен или вернул что-то непонятное."""


@dataclass(frozen=True)
class DailyForecast:
    day: date
    temp_min: float | None
    wind_max: float | None  # м/с
    precipitation: float | None  # мм, дождь + снег


@dataclass(frozen=True)
class Thresholds:
    temp_min: float = -10.0
    wind_max: float = 10.0


@dataclass(frozen=True)
class WeatherCheck:
    day: date
    forecast: DailyForecast | None = None
    reasons: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def has_warnings(self) -> bool:
        return bool(self.reasons)


def fmt_temp(v: float) -> str:
    """-18 → «-18 °C», 3 → «+3 °C», -0.2 → «0 °C»."""
    n = round(v)
    return f"{n:+d} °C" if n else "0 °C"


def check_thresholds(f: DailyForecast, t: Thresholds) -> list[str]:
    """Причины для предупреждения. Отсутствующие в прогнозе значения пропускаем."""
    reasons = []
    if f.temp_min is not None and f.temp_min < t.temp_min:
        reasons.append(f"холод до {fmt_temp(f.temp_min)} (порог {fmt_temp(t.temp_min)})")
    if f.wind_max is not None and f.wind_max > t.wind_max:
        reasons.append(f"ветер до {f.wind_max:.0f} м/с (порог {t.wind_max:.0f} м/с)")
    if f.precipitation is not None and f.precipitation > 0:
        reasons.append(f"осадки {f.precipitation:.1f} мм")
    return reasons


class OpenMeteoClient:
    def __init__(self, lat: float, lon: float, http: httpx.AsyncClient) -> None:
        self._lat, self._lon = lat, lon
        self._http = http

    async def daily(self, start: date, end: date) -> dict[date, DailyForecast]:
        params = {
            "latitude": self._lat,
            "longitude": self._lon,
            "daily": ",".join(DAILY_FIELDS),
            "wind_speed_unit": "ms",  # по умолчанию км/ч
            "timezone": "auto",
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
        }
        try:
            resp = await self._http.get(OPEN_METEO_URL, params=params)
            resp.raise_for_status()
            daily = resp.json()["daily"]
            return {
                date.fromisoformat(d): DailyForecast(date.fromisoformat(d), tmin, wind, prec)
                for d, tmin, wind, prec in zip(
                    daily["time"], *(daily[f] for f in DAILY_FIELDS), strict=True
                )
            }
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise WeatherError(f"{type(exc).__name__}: {exc}") from exc


class WeatherService:
    """Проверка погоды для задач. Один запрос на весь горизонт, кэш на 30 минут."""

    def __init__(self, client: OpenMeteoClient, thresholds: Thresholds) -> None:
        self._client = client
        self.thresholds = thresholds
        self._cache: dict[date, DailyForecast] = {}
        self._cache_key: date | None = None
        self._cache_time = 0.0

    async def _forecast(self, today: date) -> dict[date, DailyForecast]:
        fresh = time.monotonic() - self._cache_time < CACHE_TTL_S
        if not (fresh and self._cache_key == today):
            self._cache = await self._client.daily(today, today + timedelta(days=MAX_DAYS_AHEAD))
            self._cache_key, self._cache_time = today, time.monotonic()
        return self._cache

    async def check_day(self, day: date, today: date) -> WeatherCheck | None:
        """None — день вне горизонта прогноза (прошлое или дальше 10 дней)."""
        if not (today <= day <= today + timedelta(days=MAX_DAYS_AHEAD)):
            return None
        try:
            forecast = (await self._forecast(today)).get(day)
        except WeatherError as exc:
            log.warning("Open-Meteo недоступен: %s", exc)
            return WeatherCheck(day=day, error=str(exc))
        if forecast is None:
            return WeatherCheck(day=day, error="нет данных на эту дату")
        return WeatherCheck(day=day, forecast=forecast, reasons=check_thresholds(forecast, self.thresholds))

    async def check_task(self, task: Task, today: date) -> WeatherCheck | None:
        """Проверка только для уличных работ (монтаж/демонтаж)."""
        if not task.is_outdoor:
            return None
        return await self.check_day(task.due.date(), today)

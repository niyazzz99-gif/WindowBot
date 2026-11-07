"""Настройки приложения. Читаются только из переменных окружения / .env."""

import logging
import os
import re
from datetime import time
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator


class Settings(BaseModel):
    bot_token: SecretStr

    # Хранилище: sheets — Google Sheets + локальная копия в SQLite, sqlite — только локально
    storage_backend: Literal["sheets", "sqlite"] = "sqlite"
    # Путь к JSON-ключу сервисного аккаунта или сам JSON одной строкой
    google_credentials_json: str | None = None
    spreadsheet_id: str | None = None
    sqlite_path: Path = Path("data/tasks.db")

    admin_chat_id: int | None = None

    weather_lat: float = Field(ge=-90, le=90)
    weather_lon: float = Field(ge=-180, le=180)
    temp_threshold: float = -10.0  # °C, предупреждаем если минимум ниже
    wind_threshold: float = 10.0  # м/с, предупреждаем если максимум выше

    digest_time: time = time(9, 0)
    sync_interval_minutes: int = Field(5, ge=1)
    log_level: str = "INFO"

    @field_validator("log_level")
    @classmethod
    def _check_log_level(cls, v: str) -> str:
        v = v.upper()
        if v not in logging.getLevelNamesMapping():
            raise ValueError(f"неизвестный уровень логирования: {v}")
        return v

    @field_validator("spreadsheet_id")
    @classmethod
    def _extract_spreadsheet_id(cls, v: str | None) -> str | None:
        # Разрешаем вставить ссылку на таблицу целиком
        if v and (m := re.search(r"/spreadsheets/d/([\w-]+)", v)):
            return m[1]
        return v

    @model_validator(mode="after")
    def _check_sheets(self) -> "Settings":
        if self.storage_backend == "sheets" and not (
            self.google_credentials_json and self.spreadsheet_id
        ):
            raise ValueError(
                "для STORAGE_BACKEND=sheets нужны GOOGLE_CREDENTIALS_JSON и SPREADSHEET_ID"
            )
        return self


def load_settings(env_file: str | Path = ".env") -> Settings:
    """Загружает .env (если есть) и собирает Settings из окружения.

    Пустые значения пропускаются, чтобы сработали значения по умолчанию.
    """
    load_dotenv(env_file)
    raw = {
        name: os.environ[name.upper()]
        for name in Settings.model_fields
        if os.environ.get(name.upper(), "").strip()
    }
    return Settings.model_validate(raw)

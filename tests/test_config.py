from datetime import time

import pytest
from pydantic import ValidationError

from bot.config import Settings, load_settings

BASE = {"bot_token": "123:abc", "weather_lat": "55.75", "weather_lon": "37.61"}


def test_defaults():
    s = Settings.model_validate(BASE)
    assert s.storage_backend == "sqlite"
    assert (s.temp_threshold, s.wind_threshold) == (-10.0, 10.0)
    assert s.digest_time == time(9, 0)


def test_sheets_requires_credentials():
    with pytest.raises(ValidationError, match="GOOGLE_CREDENTIALS_JSON"):
        Settings.model_validate({**BASE, "storage_backend": "sheets"})


def test_bad_log_level():
    with pytest.raises(ValidationError):
        Settings.model_validate({**BASE, "log_level": "loud"})


def test_load_from_env_file(tmp_path, monkeypatch):
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    env = tmp_path / ".env"
    env.write_text(
        "BOT_TOKEN=123:abc\nWEATHER_LAT=55.75\nWEATHER_LON=37.61\n"
        "ADMIN_CHAT_ID=\nDIGEST_TIME=08:30\nTEMP_THRESHOLD=-15\nLOG_LEVEL=debug\n",
        encoding="utf-8",
    )
    s = load_settings(env)
    assert s.admin_chat_id is None  # пустое значение → default
    assert s.digest_time == time(8, 30)
    assert s.temp_threshold == -15
    assert s.log_level == "DEBUG"
    assert s.bot_token.get_secret_value() == "123:abc"

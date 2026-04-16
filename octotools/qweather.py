from __future__ import annotations

import base64
import json
import logging
import time
from pathlib import Path
from typing import Any, Literal

import httpx
from cryptography.hazmat.primitives import serialization
from pydantic import BaseModel
from pydantic_ai import RunContext
from pydantic_settings import BaseSettings, SettingsConfigDict, YamlConfigSettingsSource

from octomate.tentacles.agent.skills import SkillManager

logger = logging.getLogger(__name__)

http_client = httpx.AsyncClient(
    timeout=30.0,
    follow_redirects=True,
    headers={"Accept-Encoding": "gzip"},
)


class QWeatherConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="QWEATHER_",
        yaml_file=["octomate.default.yaml", "octomate.yaml"],
        yaml_config_section="qweather",
    )

    api_host: str = "devapi.qweather.com"
    key_id: str = ""
    project_id: str = ""
    private_key_path: Path = Path("./qweather/ed25519-private.pem")
    default_location: str = "101020100"

    @classmethod
    def settings_customise_sources(cls, settings_cls, **kwargs):
        return (
            kwargs["init_settings"],
            kwargs["env_settings"],
            YamlConfigSettingsSource(settings_cls),
            kwargs["file_secret_settings"],
        )


class WeatherDaily(BaseModel):
    fx_date: str
    temp_max: str
    temp_min: str
    text_day: str
    text_night: str
    wind_dir_day: str
    wind_scale_day: str
    humidity: str
    precip: str
    sunrise: str | None = None
    sunset: str | None = None


class WeatherForecastResult(BaseModel):
    location: str
    update_time: str
    daily: list[WeatherDaily]


def _generate_jwt(config: QWeatherConfig) -> str:
    private_key_pem = config.private_key_path.read_text()
    iat = int(time.time()) - 30

    def b64url(data: dict) -> str:
        return (
            base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode())
            .rstrip(b"=")
            .decode()
        )

    header = {"alg": "EdDSA", "kid": config.key_id}
    payload = {"sub": config.project_id, "iat": iat, "exp": iat + 900}
    signing_input = f"{b64url(header)}.{b64url(payload)}"

    private_key = serialization.load_pem_private_key(
        private_key_pem.encode(), password=None
    )
    signature = private_key.sign(signing_input.encode())  # type: ignore[call-arg]
    signature_b64 = base64.urlsafe_b64encode(signature).rstrip(b"=").decode()

    return f"{signing_input}.{signature_b64}"


async def _fetch_forecast(
    config: QWeatherConfig,
    location: str | None = None,
    days: Literal["3d", "7d"] = "3d",
) -> WeatherForecastResult:
    if not config.key_id or not config.project_id:
        raise ValueError("QWeather API credentials not configured")

    location = location or config.default_location
    response = await http_client.get(
        f"https://{config.api_host}/v7/weather/{days}",
        params={"location": location, "lang": "zh", "unit": "m"},
        headers={"Authorization": f"Bearer {_generate_jwt(config)}"},
    )
    response.raise_for_status()
    data = response.json()

    if data.get("code") != "200":
        raise ValueError(f"QWeather API error: {data.get('code')}")

    return WeatherForecastResult(
        location=location,
        update_time=data.get("updateTime", ""),
        daily=[
            WeatherDaily(
                fx_date=d.get("fxDate", ""),
                temp_max=d.get("tempMax", ""),
                temp_min=d.get("tempMin", ""),
                text_day=d.get("textDay", ""),
                text_night=d.get("textNight", ""),
                wind_dir_day=d.get("windDirDay", ""),
                wind_scale_day=d.get("windScaleDay", ""),
                humidity=d.get("humidity", ""),
                precip=d.get("precip", ""),
                sunrise=d.get("sunrise"),
                sunset=d.get("sunset"),
            )
            for d in data.get("daily", [])
        ],
    )


def _format_forecast(forecast: WeatherForecastResult, default_location: str) -> str:
    location_name = (
        "Shanghai" if forecast.location == default_location else forecast.location
    )
    lines = [f"Weather forecast for {location_name}"]

    for day in forecast.daily:
        parts = [
            f"\n{day.fx_date}",
            f"Temperature: {day.temp_min}°C ~ {day.temp_max}°C",
            f"Day: {day.text_day}  |  Night: {day.text_night}",
            f"Humidity: {day.humidity}%  |  Wind: {day.wind_dir_day} scale {day.wind_scale_day}",
        ]
        if day.precip and float(day.precip) > 0:
            parts.append(f"Precipitation: {day.precip}mm")
        if day.sunrise and day.sunset:
            parts.append(f"Sunrise: {day.sunrise}  Sunset: {day.sunset}")
        lines.append("\n".join(parts))

    return "\n".join(lines)


def register(manager: SkillManager) -> None:
    config = QWeatherConfig()
    ts = manager.register(
        name="qweather",
        description=(
            "Real-time weather forecasts powered by QWeather API. "
            "Provides 3-day or 7-day daily forecasts including temperature, "
            "humidity, wind, precipitation, and sunrise/sunset times for any "
            "city or coordinate. Default location is Shanghai."
        ),
    )

    @ts.tool
    async def get_weather(
        ctx: RunContext[Any],
        location: str | None = None,
        days: Literal["3d", "7d"] = "3d",
    ) -> str:
        """Fetch a daily weather forecast for a given location.

        Use this tool when the user asks about weather conditions, temperature,
        rain, wind, or outdoor planning for a specific city or region.

        Do NOT use this tool for:
        - Historical weather data or climate statistics
        - Real-time minute-by-minute weather updates
        - Air quality, UV index, or pollen counts
        - Weather alerts or severe weather warnings

        Args:
            ctx: Run context.
            location: A QWeather location ID (e.g. "101020100" for Shanghai)
                or longitude,latitude coordinates (e.g. "121.47,31.23").
                Defaults to Shanghai if omitted.
            days: Forecast period. "3d" for a 3-day forecast or "7d" for
                a 7-day forecast.

        Returns:
            A human-readable multi-day weather forecast string.
        """
        try:
            forecast = await _fetch_forecast(config, location=location, days=days)
            return _format_forecast(forecast, config.default_location)
        except Exception as e:
            logger.error("Failed to get weather: %s", e)
            return f"Failed to fetch weather data: {e}"

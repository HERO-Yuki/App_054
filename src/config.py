"""config.yaml と環境変数(Secrets)の読み込み。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

REQUIRED_SECRETS: tuple[str, ...] = (
    "STEAM_API_KEY",
    "STEAM_ID",
    "DISCORD_WEBHOOK_URL",
)

DEFAULTS: dict[str, Any] = {
    "country_code": "jp",
    "language": "japanese",
    "min_discount": 20,
    "max_notify": 30,
    "first_run_summary": True,
    "request_interval_sec": 1.2,
}


class ConfigError(Exception):
    """config.yaml の内容が不正な場合に送出する。"""


class MissingSecretsError(Exception):
    """必須の環境変数が未設定の場合に送出する。"""

    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        super().__init__(
            "必須の環境変数が未設定です: " + ", ".join(missing) + "\n"
            "GitHub リポジトリの Settings → Secrets and variables → Actions に登録してください。\n"
            "(Codespaces / Dependabot のタブではなく Actions タブであることを確認してください)"
        )


@dataclass(frozen=True)
class Config:
    country_code: str
    language: str
    min_discount: int
    max_notify: int
    first_run_summary: bool
    request_interval_sec: float


@dataclass(frozen=True)
class Secrets:
    steam_api_key: str
    steam_id: str
    discord_webhook_url: str
    # 未設定時は discord_webhook_url と同じ値を入れる
    discord_error_webhook_url: str

    def values(self) -> list[str]:
        """マスク対象となる秘密値の一覧。"""
        return [
            self.steam_api_key,
            self.steam_id,
            self.discord_webhook_url,
            self.discord_error_webhook_url,
        ]


def load_config(path: str | Path = "config.yaml") -> Config:
    """config.yaml を読み込む。ファイルや項目が無い場合は既定値で補う。"""
    data: dict[str, Any] = {}
    config_path = Path(path)
    if config_path.exists():
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, dict):
            raise ConfigError(f"{config_path} の形式が不正です(キーと値のマッピングではありません)")
        data = loaded

    unknown = set(data) - set(DEFAULTS)
    if unknown:
        raise ConfigError(
            f"{config_path} に不明な設定項目があります: {', '.join(sorted(unknown))}"
        )

    merged = {**DEFAULTS, **data}
    try:
        return Config(
            country_code=str(merged["country_code"]),
            language=str(merged["language"]),
            min_discount=_as_int(merged["min_discount"], "min_discount"),
            max_notify=_as_int(merged["max_notify"], "max_notify"),
            first_run_summary=_as_bool(merged["first_run_summary"], "first_run_summary"),
            request_interval_sec=_as_float(merged["request_interval_sec"], "request_interval_sec"),
        )
    except ConfigError:
        raise
    except Exception as exc:  # 想定外の型など
        raise ConfigError(f"{config_path} の読み込みに失敗しました: {exc}") from exc


def load_secrets(environ: Mapping[str, str] | None = None) -> Secrets:
    """環境変数から Secrets を読み込む。不足があれば変数名を列挙して例外を送出する。"""
    env = os.environ if environ is None else environ
    missing = [name for name in REQUIRED_SECRETS if not env.get(name, "").strip()]
    if missing:
        raise MissingSecretsError(missing)

    webhook = env["DISCORD_WEBHOOK_URL"].strip()
    error_webhook = env.get("DISCORD_ERROR_WEBHOOK_URL", "").strip() or webhook
    return Secrets(
        steam_api_key=env["STEAM_API_KEY"].strip(),
        steam_id=env["STEAM_ID"].strip(),
        discord_webhook_url=webhook,
        discord_error_webhook_url=error_webhook,
    )


def _as_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{name} は整数で指定してください(現在値: {value!r})")
    return value


def _as_float(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{name} は数値で指定してください(現在値: {value!r})")
    return float(value)


def _as_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{name} は true / false で指定してください(現在値: {value!r})")
    return value

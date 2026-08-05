"""config.py のテスト。ネットワーク・実環境変数には依存しない。"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.config import (
    ConfigError,
    MissingSecretsError,
    load_config,
    load_secrets,
)

FULL_ENV = {
    "STEAM_API_KEY": "TESTKEY1234567890",
    "STEAM_ID": "76561198000000000",
    "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/111/abc",
}


class TestLoadConfig:
    def test_missing_file_uses_defaults(self, tmp_path: Path) -> None:
        config = load_config(tmp_path / "no_such_config.yaml")
        assert config.country_code == "jp"
        assert config.language == "japanese"
        assert config.min_discount == 20
        assert config.max_notify == 30
        assert config.first_run_summary is True
        assert config.request_interval_sec == pytest.approx(1.2)

    def test_partial_file_merged_with_defaults(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("min_discount: 50\ncountry_code: us\n", encoding="utf-8")
        config = load_config(path)
        assert config.min_discount == 50
        assert config.country_code == "us"
        assert config.max_notify == 30  # 既定値が残る

    def test_unknown_key_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("mindiscount: 50\n", encoding="utf-8")  # タイポを想定
        with pytest.raises(ConfigError, match="mindiscount"):
            load_config(path)

    def test_wrong_type_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("min_discount: many\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="min_discount"):
            load_config(path)

    def test_repo_default_config_is_valid(self) -> None:
        # リポジトリ同梱の config.yaml が既定値と一致していること
        config = load_config(Path(__file__).parent.parent / "config.yaml")
        assert config.min_discount == 20
        assert config.request_interval_sec == pytest.approx(1.2)


class TestLoadSecrets:
    def test_all_missing_lists_every_name(self) -> None:
        with pytest.raises(MissingSecretsError) as exc_info:
            load_secrets(environ={})
        message = str(exc_info.value)
        assert exc_info.value.missing == [
            "STEAM_API_KEY",
            "STEAM_ID",
            "DISCORD_WEBHOOK_URL",
        ]
        for name in ("STEAM_API_KEY", "STEAM_ID", "DISCORD_WEBHOOK_URL"):
            assert name in message

    def test_partially_missing_lists_only_missing(self) -> None:
        env = {**FULL_ENV}
        del env["STEAM_ID"]
        with pytest.raises(MissingSecretsError) as exc_info:
            load_secrets(environ=env)
        assert exc_info.value.missing == ["STEAM_ID"]

    def test_blank_value_treated_as_missing(self) -> None:
        env = {**FULL_ENV, "DISCORD_WEBHOOK_URL": "   "}
        with pytest.raises(MissingSecretsError) as exc_info:
            load_secrets(environ=env)
        assert exc_info.value.missing == ["DISCORD_WEBHOOK_URL"]

    def test_error_webhook_falls_back_to_main_webhook(self) -> None:
        secrets = load_secrets(environ=FULL_ENV)
        assert secrets.discord_error_webhook_url == secrets.discord_webhook_url

    def test_error_webhook_used_when_set(self) -> None:
        env = {
            **FULL_ENV,
            "DISCORD_ERROR_WEBHOOK_URL": "https://discord.com/api/webhooks/222/err",
        }
        secrets = load_secrets(environ=env)
        assert secrets.discord_error_webhook_url == "https://discord.com/api/webhooks/222/err"

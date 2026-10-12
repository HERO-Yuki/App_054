"""main.py の統合テスト。steam / notifier の送受信をモックし、ファイルは tmp_path を使う。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import src.main as main
from src.state import load_state
from src.steam import EmptyLibraryError, OwnedGame, PriceInfo

ENV = {
    "STEAM_API_KEY": "TESTKEY1234567890",
    "STEAM_ID": "76561198000000000",
    "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/111/abc",
}


@pytest.fixture(autouse=True)
def secrets_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("DISCORD_ERROR_WEBHOOK_URL", raising=False)


@pytest.fixture()
def sent_messages(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """notifier.send_messages を捕捉し、送られたメッセージを平坦なリストで記録する。"""
    sent: list[dict[str, Any]] = []

    def fake_send(webhook_url: str, messages: Any, *, dry_run: bool = False, session: Any = None) -> None:
        sent.extend(messages)

    monkeypatch.setattr(main.notifier, "send_messages", fake_send)
    return sent


def make_config(tmp_path: Path, **overrides: Any) -> Path:
    path = tmp_path / "config.yaml"
    lines = [f"{k}: {json.dumps(v)}" for k, v in overrides.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def patch_steam(
    monkeypatch: pytest.MonkeyPatch,
    owned: list[OwnedGame],
    prices: dict[int, PriceInfo],
    failed: set[int] | None = None,
) -> None:
    monkeypatch.setattr(main.steam, "get_owned_games", lambda *a, **kw: owned)
    monkeypatch.setattr(main.steam, "get_prices", lambda *a, **kw: (prices, failed or set()))


def price(appid: int, discount: int) -> PriceInfo:
    return PriceInfo(appid, discount, 200000, 100000, "¥ 2,000", "¥ 1,000")


OWNED = [OwnedGame(1, "Game A", 0), OwnedGame(2, "Game B", 0), OwnedGame(3, "Game C", 0)]
PRICES = {1: price(1, 90), 2: price(2, 50), 3: price(3, 0)}  # 3はセールなし


class TestRun:
    def test_first_run_sends_summary_and_records_all(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sent_messages: list[dict[str, Any]]
    ) -> None:
        patch_steam(monkeypatch, OWNED, PRICES)
        state_path = tmp_path / "state.json"
        config_path = make_config(tmp_path)

        assert main.run(state_path=state_path, config_path=config_path) == 0

        assert len(sent_messages) == 1
        assert "セットアップ完了" in sent_messages[0]["content"]
        saved = load_state(state_path)
        assert saved is not None
        assert set(saved["games"]) == {"1", "2"}  # セール中全件を記録(決定F)
        assert saved["last_run_status"] == "success"
        assert saved["last_run"]  # キープアライブ

    def test_second_run_same_data_sends_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sent_messages: list[dict[str, Any]]
    ) -> None:
        patch_steam(monkeypatch, OWNED, PRICES)
        state_path = tmp_path / "state.json"
        config_path = make_config(tmp_path)

        main.run(state_path=state_path, config_path=config_path)
        first_last_run = load_state(state_path)["last_run"]  # type: ignore[index]
        sent_messages.clear()

        main.run(state_path=state_path, config_path=config_path)

        assert sent_messages == []  # 冪等性(チェックリスト項目)
        saved = load_state(state_path)
        assert saved is not None
        assert saved["last_run"] >= first_last_run  # 通知ゼロでも last_run は更新

    def test_all_sales_notified_at_once_without_omission(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sent_messages: list[dict[str, Any]]
    ) -> None:
        # 件数の上限は廃止: 一気に多数のセールが来ても全件を通知し、全件を通知済みにする
        owned = [OwnedGame(i, f"G{i}", 0) for i in range(1, 46)]
        prices = {i: price(i, 100 - i) for i in range(1, 46)}  # 99%〜55%
        patch_steam(monkeypatch, owned, prices)
        state_path = tmp_path / "state.json"
        config_path = make_config(tmp_path, first_run_summary=False)

        main.run(state_path=state_path, config_path=config_path)

        titles = [e["title"] for m in sent_messages for e in m["embeds"]]
        assert len(titles) == 45  # 全件
        assert all("他" not in m.get("content", "") for m in sent_messages)
        saved = load_state(state_path)
        assert saved is not None
        assert len(saved["games"]) == 45  # 全件が通知済み

        sent_messages.clear()
        main.run(state_path=state_path, config_path=config_path)
        assert sent_messages == []  # 2回目は新規なし

    def test_dry_run_does_not_save_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sent_messages: list[dict[str, Any]]
    ) -> None:
        patch_steam(monkeypatch, OWNED, PRICES)
        state_path = tmp_path / "state.json"
        config_path = make_config(tmp_path)

        main.run(dry_run=True, state_path=state_path, config_path=config_path)

        assert not state_path.exists()

    def test_first_run_summary_disabled_sends_individual(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sent_messages: list[dict[str, Any]]
    ) -> None:
        patch_steam(monkeypatch, OWNED, PRICES)
        config_path = make_config(tmp_path, first_run_summary=False)

        main.run(state_path=tmp_path / "state.json", config_path=config_path)

        assert len(sent_messages[0]["embeds"]) == 2
        assert "セットアップ完了" not in sent_messages[0].get("content", "")


class TestUpdateStateCmd:
    def test_creates_file_when_missing(self, tmp_path: Path) -> None:
        state_path = tmp_path / "state.json"
        assert main.update_state_cmd("failed", state_path=state_path) == 0
        saved = load_state(state_path)
        assert saved is not None
        assert saved["last_run_status"] == "failed"
        assert saved["games"] == {}

    def test_keeps_games_and_updates_status(self, tmp_path: Path) -> None:
        state_path = tmp_path / "state.json"
        state_path.write_text(
            json.dumps({"schema_version": 1, "last_run": "old", "last_run_status": "success",
                        "games": {"620": {"name": "Portal 2", "discount_percent": 90,
                                          "final_price": 20500, "notified_at": "old"}}}),
            encoding="utf-8",
        )
        main.update_state_cmd("failed", state_path=state_path)
        saved = load_state(state_path)
        assert saved is not None
        assert saved["last_run_status"] == "failed"
        assert saved["last_run"] != "old"
        assert "620" in saved["games"]

    def test_survives_corrupt_state_file(self, tmp_path: Path) -> None:
        # キープアライブ最優先: 壊れた state.json でも失敗しない
        state_path = tmp_path / "state.json"
        state_path.write_text("{ broken", encoding="utf-8")
        assert main.update_state_cmd("failed", state_path=state_path) == 0
        assert load_state(state_path) is not None


class TestCliErrors:
    def test_run_failure_returns_1_and_writes_sanitized_summary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*a: Any, **kw: Any) -> Any:
            raise EmptyLibraryError("所有ゲーム一覧が空で返りました。")

        monkeypatch.setattr(main.steam, "get_owned_games", boom)
        monkeypatch.chdir(tmp_path)  # last_error.txt / state.json / config.yaml を tmp に隔離
        monkeypatch.setattr(main, "STATE_PATH", tmp_path / "state.json")
        monkeypatch.setattr(main, "CONFIG_PATH", tmp_path / "config.yaml")
        monkeypatch.setattr(main, "ERROR_SUMMARY_PATH", tmp_path / "last_error.txt")

        assert main.cli(["run"]) == 1

        summary = (tmp_path / "last_error.txt").read_text(encoding="utf-8")
        assert "所有ゲーム一覧が空" in summary
        assert ENV["STEAM_API_KEY"] not in summary

    def test_missing_secrets_lists_names(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        for name in ENV:
            monkeypatch.delenv(name)
        monkeypatch.setattr(main, "CONFIG_PATH", tmp_path / "config.yaml")
        assert main.cli(["run"]) == 1
        err = capsys.readouterr().err
        assert "STEAM_API_KEY" in err and "STEAM_ID" in err and "DISCORD_WEBHOOK_URL" in err


class TestNotifyErrorCmd:
    def test_sends_summary_from_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sent_messages: list[dict[str, Any]]
    ) -> None:
        error_file = tmp_path / "last_error.txt"
        error_file.write_text("GetOwnedGames の取得に失敗しました\n", encoding="utf-8")
        monkeypatch.setattr(main, "ERROR_SUMMARY_PATH", error_file)

        run_url = "https://github.com/u/r/actions/runs/9"
        assert main.cli(["notify-error", "--run-url", run_url]) == 0

        description = sent_messages[0]["embeds"][0]["description"]
        assert "GetOwnedGames" in description
        assert run_url in description

    def test_generic_message_when_no_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sent_messages: list[dict[str, Any]]
    ) -> None:
        monkeypatch.setattr(main, "ERROR_SUMMARY_PATH", tmp_path / "none.txt")
        main.cli(["notify-error", "--run-url", "https://example.com/run"])
        assert "失敗しました" in sent_messages[0]["embeds"][0]["description"]

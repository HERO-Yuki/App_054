"""steam.py のテスト。ネットワークには一切接続せず、セッションをモックする。"""

from __future__ import annotations

from typing import Any

import pytest

import src.steam as steam
from src.steam import (
    BATCH_SIZE,
    EmptyLibraryError,
    OwnedGame,
    PriceInfo,
    SteamApiError,
    get_owned_games,
    get_prices,
)

API_KEY = "SECRETAPIKEY1234567890"
STEAM_ID = "76561198000000000"


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: Any = None) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers: dict[str, str] = {}

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    """あらかじめ積んだ応答(または例外)を順に返すセッション。"""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> Any:
        self.calls.append({"url": url, "params": dict(params or {})})
        result = self._responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """テスト中は待機を実際には行わず、待機秒数だけ記録する。"""
    slept: list[float] = []
    monkeypatch.setattr(steam.time, "sleep", slept.append)
    return slept


def owned_games_payload(games: list[dict[str, Any]]) -> dict[str, Any]:
    return {"response": {"game_count": len(games), "games": games}}


def price_entry(discount: int, final: int = 100000) -> dict[str, Any]:
    return {
        "success": True,
        "data": {
            "price_overview": {
                "currency": "JPY",
                "initial": 200000,
                "final": final,
                "discount_percent": discount,
                "initial_formatted": "¥ 2,000",
                "final_formatted": "¥ 1,000",
            }
        },
    }


class TestGetOwnedGames:
    def test_success(self) -> None:
        session = FakeSession([
            FakeResponse(payload=owned_games_payload([
                {"appid": 620, "name": "Portal 2", "playtime_forever": 300},
                {"appid": 440, "name": "Team Fortress 2"},
            ]))
        ])
        games = get_owned_games(API_KEY, STEAM_ID, session=session)  # type: ignore[arg-type]
        assert games == [
            OwnedGame(appid=620, name="Portal 2", playtime_forever=300),
            OwnedGame(appid=440, name="Team Fortress 2", playtime_forever=0),
        ]

    def test_empty_response_raises_with_guidance(self) -> None:
        session = FakeSession([FakeResponse(payload={"response": {}})])
        with pytest.raises(EmptyLibraryError) as exc_info:
            get_owned_games(API_KEY, STEAM_ID, session=session)  # type: ignore[arg-type]
        message = str(exc_info.value)
        assert "非公開" in message  # プライバシー設定の可能性
        assert "STEAM_ID" in message  # キー不一致・ID誤りの可能性

    def test_zero_games_raises_same_error(self) -> None:
        session = FakeSession([FakeResponse(payload=owned_games_payload([]))])
        with pytest.raises(EmptyLibraryError):
            get_owned_games(API_KEY, STEAM_ID, session=session)  # type: ignore[arg-type]

    def test_429_then_success_retries(self, no_sleep: list[float]) -> None:
        session = FakeSession([
            FakeResponse(status_code=429),
            FakeResponse(payload=owned_games_payload([{"appid": 620, "name": "Portal 2"}])),
        ])
        games = get_owned_games(API_KEY, STEAM_ID, session=session)  # type: ignore[arg-type]
        assert len(games) == 1
        assert len(no_sleep) == 1  # バックオフ待機が1回入った

    def test_persistent_429_raises_after_max_retries(self) -> None:
        session = FakeSession([FakeResponse(status_code=429)] * 10)
        with pytest.raises(SteamApiError, match="429"):
            get_owned_games(API_KEY, STEAM_ID, session=session)  # type: ignore[arg-type]
        assert len(session.calls) == steam.MAX_RETRIES + 1

    def test_403_raises_key_hint(self) -> None:
        session = FakeSession([FakeResponse(status_code=403)])
        with pytest.raises(SteamApiError, match="STEAM_API_KEY"):
            get_owned_games(API_KEY, STEAM_ID, session=session)  # type: ignore[arg-type]

    def test_error_message_never_contains_api_key(self) -> None:
        import requests

        cases: list[FakeSession] = [
            FakeSession([FakeResponse(status_code=403)]),
            FakeSession([FakeResponse(status_code=429)] * 10),
            FakeSession([requests.ConnectionError(f"url?key={API_KEY}")] * 10),
        ]
        for session in cases:
            with pytest.raises(SteamApiError) as exc_info:
                get_owned_games(API_KEY, STEAM_ID, session=session)  # type: ignore[arg-type]
            assert API_KEY not in str(exc_info.value)
            assert exc_info.value.__cause__ is None  # 連鎖経由の漏えいも防ぐ


class TestGetPrices:
    def test_batches_of_50(self, no_sleep: list[float]) -> None:
        appids = list(range(1, 121))  # 120件 → 50 + 50 + 20
        payloads = []
        for start in (0, 50, 100):
            batch = appids[start : start + 50]
            payloads.append(FakeResponse(payload={str(a): price_entry(50) for a in batch}))
        session = FakeSession(payloads)

        prices, failed = get_prices(appids, request_interval_sec=1.2, session=session)  # type: ignore[arg-type]

        assert len(session.calls) == 3
        sent_counts = [len(c["params"]["appids"].split(",")) for c in session.calls]
        assert sent_counts == [50, 50, 20]
        assert len(prices) == 120
        assert failed == set()
        assert no_sleep == [1.2, 1.2]  # バッチ間の待機(初回の前には入らない)

    def test_skips_unavailable_and_free_games(self) -> None:
        payload = {
            "620": price_entry(90, final=20500),
            "999": {"success": False},  # 販売終了・地域未対応
            "440": {"success": True, "data": []},  # 無料(price_overview なし)
        }
        session = FakeSession([FakeResponse(payload=payload)])
        prices, failed = get_prices([620, 999, 440], session=session)  # type: ignore[arg-type]
        assert set(prices) == {620}
        assert prices[620] == PriceInfo(
            appid=620,
            discount_percent=90,
            initial=200000,
            final=20500,
            initial_formatted="¥ 2,000",
            final_formatted="¥ 1,000",
        )
        assert failed == set()  # 取得自体は成功しているので failed ではない

    def test_failed_batch_goes_to_failed_not_exception(self) -> None:
        appids = list(range(1, BATCH_SIZE * 2 + 1))  # 2バッチ
        first_batch_ok = FakeResponse(
            payload={str(a): price_entry(30) for a in appids[:BATCH_SIZE]}
        )
        session = FakeSession([first_batch_ok] + [FakeResponse(status_code=429)] * 10)

        prices, failed = get_prices(appids, session=session)  # type: ignore[arg-type]

        assert len(prices) == BATCH_SIZE  # 1バッチ目は成功
        assert failed == set(appids[BATCH_SIZE:])  # 2バッチ目全件が failed

    def test_missing_key_in_response_marked_failed(self) -> None:
        session = FakeSession([FakeResponse(payload={"620": price_entry(50)})])
        prices, failed = get_prices([620, 730], session=session)  # type: ignore[arg-type]
        assert set(prices) == {620}
        assert failed == {730}

    def test_empty_appids_makes_no_request(self) -> None:
        session = FakeSession([])
        prices, failed = get_prices([], session=session)  # type: ignore[arg-type]
        assert prices == {}
        assert failed == set()
        assert session.calls == []

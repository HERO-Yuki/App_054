"""Steam API(所有ゲーム・価格)の取得。

価格取得の方式について:
appdetails の `filters=price_overview` は、カンマ区切りで複数 appid を渡しても
全件の価格が返ることを実測で確認した(2026-08-05、4件および49件のバッチで
応答キーの欠落なし)。そのため 50 件ずつのバッチ取得を採用し、逐次取得
(1件ずつ + request_interval_sec 待機)は採用していない。
バッチ間には request_interval_sec の待機を挟む。

セキュリティ上の注意:
requests の例外メッセージには URL(APIキーを含むクエリ)が入ることがあるため、
例外を外部へ伝える際は必ず自前のメッセージに置き換え、`from None` で連鎖を切る。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import requests

logger = logging.getLogger(__name__)

OWNED_GAMES_URL = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/"
APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"

BATCH_SIZE = 50
MAX_RETRIES = 3
BACKOFF_BASE_SEC = 2.0
REQUEST_TIMEOUT_SEC = 30


class SteamApiError(Exception):
    """Steam API の呼び出しに失敗した場合に送出する。"""


class EmptyLibraryError(SteamApiError):
    """所有ゲーム一覧が空で返った場合に送出する。"""


@dataclass(frozen=True)
class OwnedGame:
    appid: int
    name: str
    playtime_forever: int


@dataclass(frozen=True)
class PriceInfo:
    appid: int
    discount_percent: int
    initial: int
    final: int
    initial_formatted: str
    final_formatted: str


def get_owned_games(
    api_key: str,
    steam_id: str,
    session: requests.Session | None = None,
) -> list[OwnedGame]:
    """所有ゲーム一覧を取得する。

    空レスポンスの場合は、プライバシー設定またはキー不一致を示す
    EmptyLibraryError を送出する(FR-01)。
    """
    session = session or requests.Session()
    params = {
        "key": api_key,
        "steamid": steam_id,
        "include_appinfo": 1,
        "include_played_free_games": 1,
        "skip_unvetted_apps": "false",
        "format": "json",
    }
    try:
        data = _request_json(session, OWNED_GAMES_URL, params, "GetOwnedGames")
    except SteamApiError as exc:
        if "401" in str(exc) or "403" in str(exc):
            raise SteamApiError(
                "所有ゲーム一覧の取得が拒否されました。"
                "STEAM_API_KEY が正しく登録されているか確認してください。"
            ) from None
        raise

    response = data.get("response") or {}
    games = response.get("games")
    if not games:
        raise EmptyLibraryError(
            "所有ゲーム一覧が空で返りました。次のいずれかが原因と考えられます:\n"
            "  1. STEAM_ID が STEAM_API_KEY の持ち主と別のアカウントで、"
            "そのプロフィールの「ゲームの詳細」が非公開\n"
            "  2. STEAM_ID の値が間違っている(SteamID64 = 17桁の数字か確認)\n"
            "自分のAPIキーで自分のライブラリを見る構成であれば、プロフィール公開は不要です。"
        )

    return [
        OwnedGame(
            appid=int(g["appid"]),
            name=str(g.get("name", f"appid {g['appid']}")),
            playtime_forever=int(g.get("playtime_forever", 0)),
        )
        for g in games
    ]


def get_prices(
    appids: Sequence[int],
    *,
    country_code: str = "jp",
    language: str = "japanese",
    request_interval_sec: float = 1.2,
    session: requests.Session | None = None,
) -> tuple[dict[int, PriceInfo], set[int]]:
    """価格・割引情報を 50 件ずつのバッチで取得する。

    戻り値は (prices, failed):
    - prices: 価格が取得できたゲーム(無料・販売終了・地域未対応は含まれない)
    - failed: 通信失敗などで取得できなかった appid。
      呼び出し側(state.py)は failed のゲームの既存エントリを維持する(FR-04)。

    「取得に成功したが価格が無い」(success:false / price_overview 欠落)は
    スキップであり failed には入れない。
    """
    session = session or requests.Session()
    prices: dict[int, PriceInfo] = {}
    failed: set[int] = set()

    batches = [appids[i : i + BATCH_SIZE] for i in range(0, len(appids), BATCH_SIZE)]
    for index, batch in enumerate(batches):
        if index > 0:
            time.sleep(request_interval_sec)
        params = {
            "appids": ",".join(str(a) for a in batch),
            "cc": country_code,
            "l": language,
            "filters": "price_overview",
        }
        label = f"appdetails(バッチ {index + 1}/{len(batches)})"
        try:
            data = _request_json(session, APPDETAILS_URL, params, label)
        except SteamApiError as exc:
            logger.warning("%s: %s(このバッチをスキップし、既存の通知状態を維持します)", label, exc)
            failed.update(batch)
            continue

        for appid in batch:
            entry = data.get(str(appid))
            if not isinstance(entry, dict):
                # 応答にキー自体が無い(想定外)→ 取得失敗として扱う
                failed.add(appid)
                continue
            if not entry.get("success"):
                continue  # 販売終了・地域未対応(FR-02: スキップ)
            payload = entry.get("data")
            if not isinstance(payload, dict) or "price_overview" not in payload:
                continue  # 無料ゲームなど価格情報なし(FR-02: スキップ)
            po: Mapping[str, Any] = payload["price_overview"]
            prices[appid] = PriceInfo(
                appid=appid,
                discount_percent=int(po.get("discount_percent", 0)),
                initial=int(po.get("initial", 0)),
                final=int(po.get("final", 0)),
                initial_formatted=str(po.get("initial_formatted", "")),
                final_formatted=str(po.get("final_formatted", "")),
            )

    return prices, failed


def _request_json(
    session: requests.Session,
    url: str,
    params: Mapping[str, Any],
    label: str,
) -> dict[str, Any]:
    """GET して JSON を返す。429・5xx・接続エラーは指数バックオフでリトライする。

    例外メッセージに URL・クエリ(APIキーを含み得る)を決して含めない。
    """
    last_error = ""
    for attempt in range(MAX_RETRIES + 1):
        if attempt > 0:
            wait = BACKOFF_BASE_SEC * (2 ** (attempt - 1))
            logger.warning(
                "%s: %s — %.1f秒待機してリトライします(%d/%d)",
                label, last_error, wait, attempt, MAX_RETRIES,
            )
            time.sleep(wait)
        try:
            resp = session.get(url, params=params, timeout=REQUEST_TIMEOUT_SEC)
        except requests.RequestException as exc:
            last_error = f"接続エラー({type(exc).__name__})"
            continue

        if resp.status_code == 429:
            last_error = "レート制限(HTTP 429)"
            continue
        if resp.status_code >= 500:
            last_error = f"サーバーエラー(HTTP {resp.status_code})"
            continue
        if resp.status_code != 200:
            raise SteamApiError(f"{label} が HTTP {resp.status_code} を返しました") from None
        try:
            body = resp.json()
        except ValueError:
            last_error = "応答のJSON解析に失敗"
            continue
        if not isinstance(body, dict):
            raise SteamApiError(f"{label} の応答形式が想定外です") from None
        return body

    raise SteamApiError(
        f"{label} の取得に失敗しました(リトライ{MAX_RETRIES}回を超過): {last_error}"
    ) from None

"""state.json の読み書きと差分判定(FR-04 / FR-05)。

docs/decisions.md の決定事項:
- A: state には「通知済みの状態」のみを記録する(min_discount 未満は記録しない)
- B: 割引率が下降しても更新しない(通知済み最高割引率を保持)
- C: 価格取得に成功してセール対象外と確認できたエントリは削除、
     取得に失敗したゲームのエントリは維持
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from src.steam import OwnedGame, PriceInfo

SCHEMA_VERSION = 1


class StateError(Exception):
    """state.json が読み込めない場合に送出する。"""


@dataclass(frozen=True)
class SaleInfo:
    """セール中の所有ゲーム1件(通知とstate更新の入力)。"""

    appid: int
    name: str
    discount_percent: int
    final: int
    initial: int
    final_formatted: str
    initial_formatted: str


def load_state(path: str | Path) -> dict[str, Any] | None:
    """state.json を読み込む。ファイルが無ければ None(= 初回実行)。"""
    state_path = Path(path)
    if not state_path.exists():
        return None
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StateError(
            f"state.json の読み込みに失敗しました({type(exc).__name__})。"
            "ファイルが壊れている場合は削除してください(次回は初回実行として扱われます)。"
        ) from exc
    if not isinstance(data, dict) or not isinstance(data.get("games"), dict):
        raise StateError(
            "state.json の形式が想定外です。"
            "ファイルを削除すると次回は初回実行として扱われます。"
        )
    return data


def make_initial_state() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "last_run": "",
        "last_run_status": "",
        "games": {},
    }


def update_run_metadata(state: dict[str, Any], now: str, status: str) -> None:
    """実行結果にかかわらず毎回呼ぶ(FR-08 キープアライブ)。"""
    state["last_run"] = now
    state["last_run_status"] = status


def save_state(path: str | Path, state: dict[str, Any]) -> None:
    Path(path).write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def build_sales(
    owned: Sequence[OwnedGame],
    prices: Mapping[int, PriceInfo],
    min_discount: int,
) -> list[SaleInfo]:
    """所有ゲームと価格情報を突き合わせ、通知候補(min_discount 以上)を割引率降順で返す。"""
    sales: list[SaleInfo] = []
    for game in owned:
        price = prices.get(game.appid)
        if price is None:
            continue  # 無料・販売終了など価格情報なし
        if price.discount_percent <= 0 or price.discount_percent < min_discount:
            continue
        sales.append(
            SaleInfo(
                appid=game.appid,
                name=game.name,
                discount_percent=price.discount_percent,
                final=price.final,
                initial=price.initial,
                final_formatted=price.final_formatted,
                initial_formatted=price.initial_formatted,
            )
        )
    sales.sort(key=lambda s: (-s.discount_percent, s.appid))
    return sales


def apply_diff(
    prev_games: Mapping[str, Any],
    sales: Sequence[SaleInfo],
    fetch_failed: Iterable[int],
    now: str,
) -> tuple[list[SaleInfo], dict[str, Any]]:
    """差分判定を行い、(通知対象, 更新後の games) を返す。

    通知条件(FR-04):
    - 前回 state に無い → 通知(新規セール)
    - 割引率が前回より上昇 → 通知(さらに値下げ)
    - 同率・下降 → 通知しない(下降時も state は前回値を保持 = 決定B)

    エントリの増減:
    - 通知したゲーム → now で記録
    - sales に無く、取得にも失敗していない → 削除(セール終了・閾値未満 = 決定C)
    - 取得に失敗した appid → 既存エントリを維持(FR-04 取りこぼし回復)
    """
    failed = {str(appid) for appid in fetch_failed}
    to_notify: list[SaleInfo] = []
    new_games: dict[str, Any] = {}

    for sale in sales:
        key = str(sale.appid)
        prev = prev_games.get(key)
        if prev is None or sale.discount_percent > int(prev["discount_percent"]):
            to_notify.append(sale)
            new_games[key] = {
                "name": sale.name,
                "discount_percent": sale.discount_percent,
                "final_price": sale.final,
                "notified_at": now,
            }
        else:
            new_games[key] = dict(prev)  # 同率・下降 → 通知済み状態をそのまま保持

    current_sale_keys = {str(s.appid) for s in sales}
    for key, prev in prev_games.items():
        if key in current_sale_keys:
            continue
        if key in failed:
            new_games[key] = dict(prev)  # 取得失敗 → 維持
        # 取得成功でセール対象外 → 削除(new_games に入れない)

    return to_notify, new_games


def revert_unnotified(
    new_games: dict[str, Any],
    prev_games: Mapping[str, Any],
    omitted: Sequence[SaleInfo],
) -> None:
    """max_notify 超過で実際には通知しなかった分を「通知済み」にしない(決定D)。

    apply_diff は通知対象すべてを通知済みとして new_games に記録するため、
    件数制限で省略されたゲームは前回の状態に巻き戻す。
    → 翌日以降、枠が空けば繰り上がって通知される。
    """
    for sale in omitted:
        key = str(sale.appid)
        prev = prev_games.get(key)
        if prev is None:
            new_games.pop(key, None)
        else:
            new_games[key] = dict(prev)

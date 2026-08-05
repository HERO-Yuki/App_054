"""state.py のテスト(FR-04 / FR-05 / docs/decisions.md A〜C)。

差分判定の網羅ケース:
- 新規セール開始 → 通知対象
- 割引率が上昇 → 通知対象
- 割引率が同じ → 通知しない
- 割引率が下降 → 通知しない(state は通知済み最高割引率を維持)
- セール終了(取得成功)→ 通知せず、エントリ削除
- 閾値未満に低下(取得成功)→ エントリ削除
- API 取得に失敗したゲーム → 既存エントリを消さない
- state.json が存在しない初回 → サマリーモード
- 冪等性: 同じ入力で2回実行して2回目の通知が0件
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.state import (
    SCHEMA_VERSION,
    SaleInfo,
    StateError,
    apply_diff,
    build_sales,
    load_state,
    make_initial_state,
    save_state,
    update_run_metadata,
)
from src.steam import OwnedGame, PriceInfo

NOW = "2026-08-05T10:17:32+09:00"
LATER = "2026-08-06T10:17:32+09:00"


def sale(appid: int, name: str, discount: int, final: int = 100000) -> SaleInfo:
    return SaleInfo(
        appid=appid,
        name=name,
        discount_percent=discount,
        final=final,
        initial=200000,
        final_formatted="¥ 1,000",
        initial_formatted="¥ 2,000",
    )


def entry(name: str, discount: int, final: int = 100000, notified_at: str = NOW) -> dict:
    return {
        "name": name,
        "discount_percent": discount,
        "final_price": final,
        "notified_at": notified_at,
    }


class TestApplyDiff:
    def test_new_sale_notified(self) -> None:
        to_notify, games = apply_diff({}, [sale(620, "Portal 2", 90)], set(), NOW)
        assert [s.appid for s in to_notify] == [620]
        assert games["620"] == entry("Portal 2", 90)

    def test_increased_discount_notified(self) -> None:
        prev = {"620": entry("Portal 2", 50, notified_at="2026-08-01T10:00:00+09:00")}
        to_notify, games = apply_diff(prev, [sale(620, "Portal 2", 90)], set(), LATER)
        assert [s.appid for s in to_notify] == [620]
        assert games["620"]["discount_percent"] == 90
        assert games["620"]["notified_at"] == LATER

    def test_same_discount_not_notified(self) -> None:
        prev = {"620": entry("Portal 2", 90)}
        to_notify, games = apply_diff(prev, [sale(620, "Portal 2", 90)], set(), LATER)
        assert to_notify == []
        assert games["620"]["notified_at"] == NOW  # 変更されない

    def test_decreased_discount_not_notified_and_keeps_max(self) -> None:
        # 決定B: 90%で通知済み → 50%に下降 → 通知せず、stateは90%のまま
        prev = {"620": entry("Portal 2", 90)}
        to_notify, games = apply_diff(prev, [sale(620, "Portal 2", 50)], set(), LATER)
        assert to_notify == []
        assert games["620"]["discount_percent"] == 90
        # → その後90%に戻っても「同率」なので再通知されない

    def test_sale_ended_entry_deleted(self) -> None:
        # 決定C: 取得に成功してセール対象外と確認できたら削除
        prev = {"620": entry("Portal 2", 90)}
        to_notify, games = apply_diff(prev, [], set(), LATER)
        assert to_notify == []
        assert games == {}

    def test_fetch_failed_entry_kept(self) -> None:
        # FR-04: API 取得に失敗したゲームの既存エントリは維持
        prev = {"620": entry("Portal 2", 90)}
        to_notify, games = apply_diff(prev, [], {620}, LATER)
        assert to_notify == []
        assert games["620"] == entry("Portal 2", 90)

    def test_mixed_scenario(self) -> None:
        prev = {
            "1": entry("新規ではない・同率", 50),
            "2": entry("上昇する", 30),
            "3": entry("終了する", 80),
            "4": entry("取得失敗", 60),
        }
        sales = [
            sale(1, "新規ではない・同率", 50),
            sale(2, "上昇する", 70),
            sale(5, "新規セール", 40),
        ]
        to_notify, games = apply_diff(prev, sales, {4}, LATER)
        assert sorted(s.appid for s in to_notify) == [2, 5]
        assert set(games) == {"1", "2", "4", "5"}  # 3(終了)だけ消える

    def test_idempotency_second_run_notifies_nothing(self) -> None:
        # 非機能要件: 同じ状態で2回実行しても通知が重複しない
        sales = [sale(620, "Portal 2", 90), sale(730, "CS2", 50)]
        to_notify_1, games_1 = apply_diff({}, sales, set(), NOW)
        assert len(to_notify_1) == 2
        to_notify_2, games_2 = apply_diff(games_1, sales, set(), LATER)
        assert to_notify_2 == []
        assert games_2 == games_1


class TestBuildSales:
    def test_joins_filters_and_sorts_desc(self) -> None:
        owned = [
            OwnedGame(620, "Portal 2", 300),
            OwnedGame(730, "CS2", 0),
            OwnedGame(440, "TF2", 10),      # 価格情報なし(無料)
            OwnedGame(999, "薄い割引", 5),
        ]
        prices = {
            620: PriceInfo(620, 90, 200000, 20000, "¥ 2,000", "¥ 200"),
            730: PriceInfo(730, 0, 150000, 150000, "", "¥ 1,500"),   # セールなし
            999: PriceInfo(999, 10, 100000, 90000, "¥ 1,000", "¥ 900"),  # 閾値未満
        }
        sales = build_sales(owned, prices, min_discount=20)
        assert [s.appid for s in sales] == [620]
        assert sales[0].name == "Portal 2"

    def test_min_discount_boundary_inclusive(self) -> None:
        owned = [OwnedGame(1, "ちょうど閾値", 0)]
        prices = {1: PriceInfo(1, 20, 100000, 80000, "¥ 1,000", "¥ 800")}
        assert len(build_sales(owned, prices, min_discount=20)) == 1

    def test_sorted_by_discount_desc(self) -> None:
        owned = [OwnedGame(1, "a", 0), OwnedGame(2, "b", 0), OwnedGame(3, "c", 0)]
        prices = {
            1: PriceInfo(1, 30, 0, 0, "", ""),
            2: PriceInfo(2, 90, 0, 0, "", ""),
            3: PriceInfo(3, 60, 0, 0, "", ""),
        }
        sales = build_sales(owned, prices, min_discount=20)
        assert [s.discount_percent for s in sales] == [90, 60, 30]


class TestStateFile:
    def test_load_missing_returns_none_means_first_run(self, tmp_path: Path) -> None:
        assert load_state(tmp_path / "state.json") is None

    def test_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        state = make_initial_state()
        state["games"] = {"620": entry("Portal 2", 90)}
        update_run_metadata(state, NOW, "success")
        save_state(path, state)

        loaded = load_state(path)
        assert loaded is not None
        assert loaded["schema_version"] == SCHEMA_VERSION
        assert loaded["last_run"] == NOW
        assert loaded["last_run_status"] == "success"
        assert loaded["games"]["620"]["name"] == "Portal 2"

    def test_saved_file_is_utf8_json(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        state = make_initial_state()
        state["games"] = {"1": entry("日本語タイトル", 50)}
        save_state(path, state)
        raw = path.read_text(encoding="utf-8")
        assert "日本語タイトル" in raw  # ensure_ascii=False で人間が読める
        json.loads(raw)

    def test_corrupt_file_raises_clear_error(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        path.write_text("{ broken", encoding="utf-8")
        with pytest.raises(StateError, match="state.json"):
            load_state(path)

    def test_update_run_metadata_on_failure_keeps_games(self) -> None:
        # FR-09: 失敗時も last_run / last_run_status を更新(キープアライブ)
        state = make_initial_state()
        state["games"] = {"620": entry("Portal 2", 90)}
        update_run_metadata(state, LATER, "failed")
        assert state["last_run"] == LATER
        assert state["last_run_status"] == "failed"
        assert state["games"] == {"620": entry("Portal 2", 90)}

"""notifier.py のテスト。受け入れ基準:

所有ゲーム80件がセール中という想定データで、ドライラン出力が
Discord の制限(content 2000文字 / embed 10件 / embed合計6000文字)を超えない。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

import src.notifier as notifier
from src.notifier import (
    EMBEDS_PER_MESSAGE,
    NotifyError,
    build_error_message,
    build_sale_messages,
    build_summary_messages,
    send_messages,
)
from src.state import SaleInfo

WEBHOOK = "https://discord.com/api/webhooks/111/SECRETTOKENabcdef"


def sale(appid: int, discount: int, name: str | None = None) -> SaleInfo:
    return SaleInfo(
        appid=appid,
        name=name or f"ゲーム {appid}",
        discount_percent=discount,
        final=100000,
        initial=200000,
        final_formatted="¥ 1,000",
        initial_formatted="¥ 2,000",
    )


def eighty_sales() -> list[SaleInfo]:
    # 割引率 21〜100% の80件(わざと昇順で渡し、ソートされることも確認する)
    return [sale(appid=1000 + i, discount=21 + (i % 80)) for i in range(80)]


def assert_within_discord_limits(message: dict[str, Any]) -> None:
    content = message.get("content", "")
    embeds = message.get("embeds", [])
    assert len(content) <= 2000, f"content が2000文字を超過: {len(content)}"
    assert len(embeds) <= EMBEDS_PER_MESSAGE, f"embed が10件を超過: {len(embeds)}"
    total_chars = sum(
        len(e.get("title", "")) + len(e.get("description", "")) for e in embeds
    )
    assert total_chars <= 6000, f"embed 文字数合計が6000を超過: {total_chars}"


class TestBuildSaleMessages:
    def test_80_sales_within_limits(self) -> None:
        # 受け入れ基準の本体。max_notify=30 → 10件×3メッセージ
        messages = build_sale_messages(eighty_sales(), max_notify=30)
        assert len(messages) == 3
        for message in messages:
            assert_within_discord_limits(message)
        total_embeds = sum(len(m["embeds"]) for m in messages)
        assert total_embeds == 30

    def test_omitted_count_appended_to_last_message(self) -> None:
        messages = build_sale_messages(eighty_sales(), max_notify=30)
        assert "他 50 件" in messages[-1]["content"]
        # 途中のメッセージには付かない
        assert "他" not in messages[1].get("content", "")

    def test_sorted_by_discount_desc(self) -> None:
        messages = build_sale_messages(eighty_sales(), max_notify=30)
        discounts = []
        for message in messages:
            for embed in message["embeds"]:
                percent = int(embed["description"].split("% OFF")[0].strip("*"))
                discounts.append(percent)
        assert discounts == sorted(discounts, reverse=True)
        assert discounts[0] == 100

    def test_no_omission_note_when_under_limit(self) -> None:
        messages = build_sale_messages([sale(1, 50), sale(2, 30)], max_notify=30)
        assert len(messages) == 1
        assert "他" not in messages[0].get("content", "")
        assert len(messages[0]["embeds"]) == 2

    def test_empty_sales_returns_no_messages(self) -> None:
        assert build_sale_messages([], max_notify=30) == []

    def test_embed_contents(self) -> None:
        messages = build_sale_messages([sale(620, 90, name="Portal 2")], max_notify=30)
        embed = messages[0]["embeds"][0]
        assert embed["title"] == "Portal 2"
        assert embed["url"] == "https://store.steampowered.com/app/620/"
        assert "620" in embed["image"]["url"]
        assert "90% OFF" in embed["description"]
        assert "¥ 2,000" in embed["description"]  # 定価
        assert "¥ 1,000" in embed["description"]  # セール価格


class TestBuildSaleMessagesListStyle:
    def test_15_sales_fit_in_one_message(self) -> None:
        sales = [sale(1000 + i, 21 + i, name=f"日本語タイトル {i}") for i in range(15)]
        messages = build_sale_messages(sales, max_notify=30, style="list")
        assert len(messages) == 1
        embeds = messages[0]["embeds"]
        assert len(embeds) == 1
        lines = embeds[0]["description"].split("\n")
        assert len(lines) == 15
        assert "(15件)" in messages[0]["content"]

    def test_line_contains_all_columns(self) -> None:
        messages = build_sale_messages([sale(620, 90, name="Portal 2")], max_notify=30, style="list")
        line = messages[0]["embeds"][0]["description"]
        assert "**90%**" in line                                    # 割引率
        assert "[Portal 2](https://store.steampowered.com/app/620/)" in line  # タイトル=ストアリンク
        assert "~~¥ 2,000~~" in line                                # 定価(打ち消し線)
        assert "**¥ 1,000**" in line                                # 現在価格

    def test_sorted_desc_and_omitted_note(self) -> None:
        messages = build_sale_messages(eighty_sales(), max_notify=30, style="list")
        all_lines = [
            line
            for m in messages
            for e in m["embeds"]
            for line in e["description"].split("\n")
        ]
        assert len(all_lines) == 30
        discounts = [int(line.split("%**")[0].strip("*")) for line in all_lines]
        assert discounts == sorted(discounts, reverse=True)
        assert "他 50 件" in messages[-1]["content"]

    def test_descriptions_within_discord_limit(self) -> None:
        # 長いタイトルでも embed description の4096文字制限を超えない
        sales = [sale(1000 + i, 50, name="と" * 120 + str(i)) for i in range(80)]
        messages = build_sale_messages(sales, max_notify=80, style="list")
        for message in messages:
            assert len(message["embeds"]) <= EMBEDS_PER_MESSAGE
            for embed in message["embeds"]:
                assert len(embed["description"]) <= 4096

    def test_brackets_in_title_escaped(self) -> None:
        messages = build_sale_messages(
            [sale(1, 50, name="変な[タイトル]のゲーム")], max_notify=30, style="list"
        )
        line = messages[0]["embeds"][0]["description"]
        assert "[変な(タイトル)のゲーム](" in line  # リンク記法を壊さない


class TestBuildSummaryMessages:
    def test_first_run_summary_format(self) -> None:
        messages = build_summary_messages(eighty_sales())
        assert len(messages) == 1
        message = messages[0]
        assert "セットアップ完了" in message["content"]
        assert "80 本" in message["content"]
        assert "明日以降" in message["content"]
        assert len(message["embeds"]) == 10  # 上位10件のみ
        assert_within_discord_limits(message)

    def test_summary_with_few_sales(self) -> None:
        messages = build_summary_messages([sale(1, 50)])
        assert "1 本" in messages[0]["content"]
        assert len(messages[0]["embeds"]) == 1

    def test_summary_with_zero_sales(self) -> None:
        messages = build_summary_messages([])
        assert "0 本" in messages[0]["content"]
        assert "embeds" not in messages[0]

    def test_summary_list_style_top10_lines(self) -> None:
        messages = build_summary_messages(eighty_sales(), style="list")
        assert len(messages) == 1
        assert "セットアップ完了" in messages[0]["content"]
        lines = messages[0]["embeds"][0]["description"].split("\n")
        assert len(lines) == 10  # 上位10件のみ(FR-05は形式によらず維持)


class TestBuildErrorMessage:
    def test_contains_time_error_and_log_link(self) -> None:
        run_url = "https://github.com/user/repo/actions/runs/123"
        messages = build_error_message(
            "GetOwnedGames の取得に失敗しました", run_url, "2026-08-05T10:17:32+09:00"
        )
        description = messages[0]["embeds"][0]["description"]
        assert "2026-08-05T10:17:32+09:00" in description
        assert "GetOwnedGames" in description
        assert run_url in description


class FakeResponse:
    def __init__(self, status_code: int, headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self.headers = headers or {}


class FakeSession:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, json: Any = None, timeout: float | None = None) -> Any:
        self.calls.append({"url": url, "json": json})
        result = self._responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []
    monkeypatch.setattr(notifier.time, "sleep", slept.append)
    return slept


class TestSendMessages:
    def test_dry_run_prints_json_and_sends_nothing(self, capsys: pytest.CaptureFixture[str]) -> None:
        messages = build_sale_messages(eighty_sales(), max_notify=30)
        session = FakeSession([])
        send_messages(WEBHOOK, messages, dry_run=True, session=session)  # type: ignore[arg-type]
        captured = capsys.readouterr().out
        assert session.calls == []  # 送信していない
        payloads = json.loads(captured)  # JSONとして解析できる
        assert len(payloads) == 3
        assert WEBHOOK not in captured  # Webhook URL は出力しない

    def test_sends_each_message(self) -> None:
        session = FakeSession([FakeResponse(204), FakeResponse(204)])
        messages = build_sale_messages(eighty_sales()[:15], max_notify=30)
        send_messages(WEBHOOK, messages, session=session)  # type: ignore[arg-type]
        assert len(session.calls) == 2
        assert session.calls[0]["url"] == WEBHOOK

    def test_429_respects_retry_after_then_succeeds(self, no_sleep: list[float]) -> None:
        session = FakeSession([
            FakeResponse(429, headers={"Retry-After": "3"}),
            FakeResponse(204),
        ])
        send_messages(WEBHOOK, [{"content": "x"}], session=session)  # type: ignore[arg-type]
        assert len(session.calls) == 2
        assert 3.0 in no_sleep  # Retry-After が尊重された

    def test_4xx_raises_without_webhook_url(self) -> None:
        session = FakeSession([FakeResponse(404)])
        with pytest.raises(NotifyError) as exc_info:
            send_messages(WEBHOOK, [{"content": "x"}], session=session)  # type: ignore[arg-type]
        assert WEBHOOK not in str(exc_info.value)
        assert "DISCORD_WEBHOOK_URL" in str(exc_info.value)

    def test_persistent_failure_raises_without_webhook_url(self) -> None:
        import requests

        session = FakeSession([requests.ConnectionError(f"url={WEBHOOK}")] * 10)
        with pytest.raises(NotifyError) as exc_info:
            send_messages(WEBHOOK, [{"content": "x"}], session=session)  # type: ignore[arg-type]
        assert WEBHOOK not in str(exc_info.value)
        assert exc_info.value.__cause__ is None

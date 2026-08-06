"""Discord Webhook への通知(FR-06 / FR-09)。

Discord の制約:
- 1メッセージの content は 2000 文字まで
- 1メッセージの embed は最大 10 個
- embed 全体の文字数合計は 6000 文字まで

ペイロード生成(build_*)と送信(send_messages)を分離し、
送信せずに検証できるドライランモードを提供する。

セキュリティ上の注意:
Webhook URL を例外メッセージ・ログ・ドライラン出力に含めない。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Sequence

import requests

from src.state import SaleInfo

logger = logging.getLogger(__name__)

STORE_URL = "https://store.steampowered.com/app/{appid}/"
HEADER_IMAGE_URL = "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{appid}/header.jpg"

EMBEDS_PER_MESSAGE = 10
SUMMARY_TOP_N = 10
EMBED_COLOR = 0x66C0F4  # Steam ブルー
ERROR_COLOR = 0xE74C3C
# list形式: embed description の上限4096文字に対する安全マージン
LIST_EMBED_CHAR_BUDGET = 3500

MAX_RETRIES = 3
BACKOFF_BASE_SEC = 2.0
REQUEST_TIMEOUT_SEC = 30
MESSAGE_INTERVAL_SEC = 0.7  # 連投時のレート制限予防


class NotifyError(Exception):
    """Discord への送信に失敗した場合に送出する。"""


def build_sale_embed(sale: SaleInfo) -> dict[str, Any]:
    initial = sale.initial_formatted or "-"
    return {
        "title": sale.name[:256],
        "url": STORE_URL.format(appid=sale.appid),
        "description": f"**{sale.discount_percent}% OFF** {initial} → **{sale.final_formatted}**",
        "color": EMBED_COLOR,
        "image": {"url": HEADER_IMAGE_URL.format(appid=sale.appid)},
    }


def build_sale_line(sale: SaleInfo) -> str:
    """list形式の1行。割引率 / タイトル(ストアへのリンク) / 定価 / 現在価格。"""
    name = sale.name.replace("[", "(").replace("]", ")")  # Markdownリンクの括弧と衝突しないように
    url = STORE_URL.format(appid=sale.appid)
    price = f"**{sale.final_formatted}**"
    if sale.initial_formatted:
        price = f"~~{sale.initial_formatted}~~ → {price}"
    return f"**{sale.discount_percent}%** [{name}]({url}) {price}"


def _pack_lines_into_embeds(lines: Sequence[str]) -> list[dict[str, Any]]:
    """行のリストを、description の文字数上限を超えないよう embed に詰める。"""
    embeds: list[dict[str, Any]] = []
    buffer: list[str] = []
    size = 0
    for line in lines:
        if buffer and size + len(line) + 1 > LIST_EMBED_CHAR_BUDGET:
            embeds.append({"description": "\n".join(buffer), "color": EMBED_COLOR})
            buffer, size = [], 0
        buffer.append(line)
        size += len(line) + 1
    if buffer:
        embeds.append({"description": "\n".join(buffer), "color": EMBED_COLOR})
    return embeds


def build_sale_messages(
    sales: Sequence[SaleInfo],
    max_notify: int,
    style: str = "card",
) -> list[dict[str, Any]]:
    """通常通知のペイロード一覧を生成する。

    割引率降順に並べ、max_notify 件までを通知。
    - style="card": 1ゲーム1embed(画像付き)、embed 10個ずつのメッセージに分割
    - style="list": 1行1ゲームのコンパクト表示(リンク可・画像なし)
    上限超過分は最終メッセージの content に「他 N 件」と付記する(FR-06)。
    """
    if not sales:
        return []
    ordered = sorted(sales, key=lambda s: (-s.discount_percent, s.appid))
    shown = ordered[:max_notify]
    omitted = len(ordered) - len(shown)

    if style == "list":
        embed_groups = _chunk(_pack_lines_into_embeds([build_sale_line(s) for s in shown]))
    else:
        embed_groups = _chunk([build_sale_embed(s) for s in shown])

    messages: list[dict[str, Any]] = []
    for index, chunk in enumerate(embed_groups):
        content = ""
        if index == 0:
            content = f"🎮 所有ゲームがセール中です({len(ordered)}件)"
        if index == len(embed_groups) - 1 and omitted > 0:
            suffix = f"…他 {omitted} 件は省略しました"
            content = f"{content}\n{suffix}" if content else suffix
        message: dict[str, Any] = {"embeds": chunk}
        if content:
            message["content"] = content
        messages.append(message)
    return messages


def _chunk(embeds: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    return [embeds[i : i + EMBEDS_PER_MESSAGE] for i in range(0, len(embeds), EMBEDS_PER_MESSAGE)]


def build_summary_messages(
    sales: Sequence[SaleInfo],
    style: str = "card",
) -> list[dict[str, Any]]:
    """初回実行時のサマリー通知(FR-05)。割引率上位10件のみ列挙する。"""
    ordered = sorted(sales, key=lambda s: (-s.discount_percent, s.appid))
    top = ordered[:SUMMARY_TOP_N]
    content = (
        f"✅ セットアップ完了。現在 {len(ordered)} 本の所有ゲームがセール中です。\n"
        "明日以降は、新しくセールが始まったもの・さらに値下げされたものだけをお知らせします。"
    )
    if len(ordered) > len(top):
        content += f"\n(以下は割引率上位 {len(top)} 件です)"
    message: dict[str, Any] = {"content": content}
    if top:
        if style == "list":
            message["embeds"] = _pack_lines_into_embeds([build_sale_line(s) for s in top])
        else:
            message["embeds"] = [build_sale_embed(s) for s in top]
    return [message]


def build_error_message(error_summary: str, run_url: str, now: str) -> list[dict[str, Any]]:
    """実行失敗時のエラー通知(FR-09)。発生日時・エラー種別・実行ログへのリンクを含む。"""
    embed = {
        "title": "⚠️ Steam Sale Notifier の実行に失敗しました",
        "description": (
            f"発生日時: {now}\n"
            f"エラー: {error_summary[:1500]}\n"
            f"[実行ログを開く]({run_url})"
        ),
        "color": ERROR_COLOR,
    }
    return [{"embeds": [embed]}]


def send_messages(
    webhook_url: str,
    messages: Sequence[dict[str, Any]],
    *,
    dry_run: bool = False,
    session: requests.Session | None = None,
) -> None:
    """メッセージを順に送信する。dry_run=True なら送信せずペイロードを JSON 出力する。"""
    if dry_run:
        print(json.dumps(list(messages), ensure_ascii=False, indent=2))
        return

    session = session or requests.Session()
    for index, payload in enumerate(messages):
        if index > 0:
            time.sleep(MESSAGE_INTERVAL_SEC)
        _post_with_retry(session, webhook_url, payload, f"メッセージ {index + 1}/{len(messages)}")
    logger.info("Discord へ %d 件のメッセージを送信しました", len(messages))


def _post_with_retry(
    session: requests.Session,
    webhook_url: str,
    payload: dict[str, Any],
    label: str,
) -> None:
    """429(Retry-After 尊重)・5xx・接続エラーを指数バックオフでリトライする。

    例外メッセージに Webhook URL を決して含めない。
    """
    last_error = ""
    for attempt in range(MAX_RETRIES + 1):
        if attempt > 0:
            wait = BACKOFF_BASE_SEC * (2 ** (attempt - 1))
            logger.warning(
                "Discord %s: %s — %.1f秒待機してリトライします(%d/%d)",
                label, last_error, wait, attempt, MAX_RETRIES,
            )
            time.sleep(wait)
        try:
            resp = session.post(webhook_url, json=payload, timeout=REQUEST_TIMEOUT_SEC)
        except requests.RequestException as exc:
            last_error = f"接続エラー({type(exc).__name__})"
            continue

        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                try:
                    time.sleep(min(float(retry_after), 30.0))
                except ValueError:
                    pass
            last_error = "レート制限(HTTP 429)"
            continue
        if resp.status_code >= 500:
            last_error = f"サーバーエラー(HTTP {resp.status_code})"
            continue
        if resp.status_code >= 400:
            raise NotifyError(
                f"Discord への送信({label})が HTTP {resp.status_code} で拒否されました。"
                "DISCORD_WEBHOOK_URL が正しいか確認してください。"
            ) from None
        return  # 200 / 204 成功

    raise NotifyError(
        f"Discord への送信({label})に失敗しました(リトライ{MAX_RETRIES}回を超過): {last_error}"
    ) from None

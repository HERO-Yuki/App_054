"""エントリポイント。

サブコマンド:
- run            所有ゲーム取得 → 価格取得 → 差分判定 → Discord 通知 → state 保存
                 (--dry-run で送信・state 更新を行わずペイロードを JSON 出力)
- update-state   last_run / last_run_status のみ更新して保存。
                 run がクラッシュした場合でも毎日コミットを絶やさないための
                 キープアライブ用(workflow の if: failure() ステップから呼ぶ)
- notify-error   エラー通知を Discord へ送信(workflow の if: failure() ステップから呼ぶ)
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from src import notifier, steam
from src import state as state_mod
from src.config import (
    Config,
    ConfigError,
    MissingSecretsError,
    Secrets,
    load_config,
    load_secrets,
)
from src.logutil import mask_text, setup_logging

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9), "JST")
STATE_PATH = Path("state.json")
CONFIG_PATH = Path("config.yaml")
# run 失敗時に notify-error が読む、Secrets を含まないエラー概要(コミットしない)
ERROR_SUMMARY_PATH = Path("last_error.txt")


def now_jst() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def run(
    dry_run: bool = False,
    state_path: Path = STATE_PATH,
    config_path: Path = CONFIG_PATH,
) -> int:
    config = load_config(config_path)
    secrets = load_secrets()
    setup_logging(secrets.values())
    logger.info("実行開始(dry_run=%s)", dry_run)

    prev_state = state_mod.load_state(state_path)
    first_run = prev_state is None
    current = prev_state or state_mod.make_initial_state()
    prev_games: dict = current["games"]

    session = requests.Session()
    owned = steam.get_owned_games(secrets.steam_api_key, secrets.steam_id, session=session)
    logger.info("所有ゲーム: %d 本", len(owned))

    prices, failed = steam.get_prices(
        [g.appid for g in owned],
        country_code=config.country_code,
        language=config.language,
        request_interval_sec=config.request_interval_sec,
        session=session,
    )
    if failed:
        logger.warning("価格取得に失敗したゲーム: %d 件(前回の通知状態を維持します)", len(failed))

    sales = state_mod.build_sales(owned, prices, config.min_discount)
    logger.info("セール中(%d%% 以上): %d 件", config.min_discount, len(sales))

    now = now_jst()
    to_notify, new_games = state_mod.apply_diff(prev_games, sales, failed, now)

    if first_run and config.first_run_summary:
        messages = notifier.build_summary_messages(sales)
        notified_count = len(sales)
    else:
        messages = notifier.build_sale_messages(to_notify, config.max_notify)
        shown = to_notify[: config.max_notify]
        state_mod.revert_unnotified(new_games, prev_games, to_notify[config.max_notify :])
        notified_count = len(shown)

    if messages:
        notifier.send_messages(secrets.discord_webhook_url, messages, dry_run=dry_run)
    logger.info("通知: %d 件(メッセージ %d 通)", notified_count, len(messages))

    current["games"] = new_games
    state_mod.update_run_metadata(current, now, "success")
    if dry_run:
        logger.info("ドライランのため state.json は更新しません")
    else:
        state_mod.save_state(state_path, current)
    return 0


def update_state_cmd(status: str, state_path: Path = STATE_PATH) -> int:
    """last_run / last_run_status のみ更新する(FR-08/FR-09 キープアライブ)。

    state.json が壊れていても失敗させない(キープアライブ最優先)。
    """
    try:
        current = state_mod.load_state(state_path)
    except state_mod.StateError:
        current = None
    current = current or state_mod.make_initial_state()
    state_mod.update_run_metadata(current, now_jst(), status)
    state_mod.save_state(state_path, current)
    print(f"state.json を更新しました(status={status})")
    return 0


def notify_error_cmd(run_url: str, dry_run: bool = False) -> int:
    secrets = load_secrets()
    setup_logging(secrets.values())
    summary = "ワークフローの実行が失敗しました。詳細は実行ログを確認してください。"
    if ERROR_SUMMARY_PATH.exists():
        text = ERROR_SUMMARY_PATH.read_text(encoding="utf-8").strip()
        if text:
            summary = text
    messages = notifier.build_error_message(summary, run_url, now_jst())
    notifier.send_messages(secrets.discord_error_webhook_url, messages, dry_run=dry_run)
    return 0


def _write_error_summary(text: str, secret_values: list[str]) -> None:
    """notify-error が使うエラー概要を書き出す。念のため Secrets をマスクする。"""
    try:
        ERROR_SUMMARY_PATH.write_text(mask_text(text, secret_values) + "\n", encoding="utf-8")
    except OSError:
        logger.warning("エラー概要ファイルの書き出しに失敗しました")


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="steam-sale-notifier")
    sub = parser.add_subparsers(dest="command")

    run_parser = sub.add_parser("run", help="セール確認と通知を実行する")
    run_parser.add_argument("--dry-run", action="store_true",
                            help="送信せずペイロードをJSON出力する(state.jsonも更新しない)")

    update_parser = sub.add_parser("update-state", help="last_run のみ更新する(キープアライブ)")
    update_parser.add_argument("--status", required=True, choices=["success", "failed"])

    error_parser = sub.add_parser("notify-error", help="エラー通知を Discord へ送る")
    error_parser.add_argument("--run-url", required=True, help="GitHub Actions 実行ログのURL")
    error_parser.add_argument("--dry-run", action="store_true")

    args = parser.parse_args(argv)
    command = args.command or "run"

    secret_values: list[str] = []
    try:
        if command == "run":
            try:
                secret_values = load_secrets().values()
            except MissingSecretsError:
                pass  # 直後の run() が同じ例外を投げる
            return run(dry_run=getattr(args, "dry_run", False))
        if command == "update-state":
            return update_state_cmd(args.status)
        return notify_error_cmd(args.run_url, dry_run=args.dry_run)
    except (ConfigError, MissingSecretsError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (steam.SteamApiError, state_mod.StateError, notifier.NotifyError) as exc:
        # これらの例外メッセージは Secrets を含まないよう設計済み(各モジュール参照)
        logger.error("%s", exc)
        _write_error_summary(str(exc), secret_values)
        return 1
    except Exception as exc:  # 想定外: メッセージに Secrets が入り得るため種別のみ残す
        logger.exception("予期しないエラーが発生しました")
        _write_error_summary(f"予期しないエラー({type(exc).__name__})", secret_values)
        return 1


if __name__ == "__main__":
    sys.exit(cli())

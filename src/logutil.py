"""Secrets を確実にマスクするロギング設定。

マスクはフォーマット済み文字列全体(例外トレースバックを含む)に対して行うため、
メッセージ・引数・例外メッセージのいずれに秘密値が紛れても出力されない。
"""

from __future__ import annotations

import logging
import sys
from typing import Iterable

MASK = "***"


class MaskingFormatter(logging.Formatter):
    """フォーマット後の文字列から秘密値を置換するフォーマッタ。"""

    def __init__(self, fmt: str, secret_values: Iterable[str]) -> None:
        super().__init__(fmt=fmt, datefmt="%Y-%m-%d %H:%M:%S")
        # 空文字を除外し、長い値から先に置換する(部分一致の取りこぼし防止)
        self._secrets = sorted((s for s in secret_values if s), key=len, reverse=True)

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for secret in self._secrets:
            text = text.replace(secret, MASK)
        return text


def setup_logging(secret_values: Iterable[str], level: int = logging.INFO) -> logging.Logger:
    """ルートロガーを設定し、全ハンドラ出力に秘密値マスクを適用する。"""
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(
        MaskingFormatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", secret_values)
    )
    root.addHandler(handler)
    return root

"""エントリポイント。

フェーズ1時点では「設定と Secrets を読み込んで検証する」ところまでを実装する。
セール取得・差分判定・通知は以降のフェーズで追加する。
"""

from __future__ import annotations

import logging
import sys

from src.config import ConfigError, MissingSecretsError, load_config, load_secrets
from src.logutil import setup_logging

logger = logging.getLogger(__name__)


def main() -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"設定エラー: {exc}", file=sys.stderr)
        return 1

    try:
        secrets = load_secrets()
    except MissingSecretsError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    setup_logging(secrets.values())
    logger.info(
        "設定を読み込みました (country=%s, min_discount=%d%%, max_notify=%d)",
        config.country_code,
        config.min_discount,
        config.max_notify,
    )
    # 以降のフェーズ: 所有ゲーム取得 → 価格取得 → 差分判定 → Discord 通知
    return 0


if __name__ == "__main__":
    sys.exit(main())

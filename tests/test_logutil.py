"""logutil.py のテスト。Secrets がログ出力から必ずマスクされることを確認する。"""

from __future__ import annotations

import io
import logging

from src.logutil import MASK, MaskingFormatter

SECRET_KEY = "TESTKEY1234567890"
SECRET_WEBHOOK = "https://discord.com/api/webhooks/111/abcdefg"


def make_logger(secrets: list[str]) -> tuple[logging.Logger, io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(MaskingFormatter("%(levelname)s %(message)s", secrets))
    logger = logging.getLogger(f"test_logutil_{id(stream)}")
    logger.setLevel(logging.DEBUG)
    logger.handlers = [handler]
    logger.propagate = False
    return logger, stream


def test_secret_in_message_is_masked() -> None:
    logger, stream = make_logger([SECRET_KEY])
    logger.info("APIキーは %s です", SECRET_KEY)
    output = stream.getvalue()
    assert SECRET_KEY not in output
    assert MASK in output


def test_multiple_secrets_masked() -> None:
    logger, stream = make_logger([SECRET_KEY, SECRET_WEBHOOK])
    logger.warning("key=%s url=%s", SECRET_KEY, SECRET_WEBHOOK)
    output = stream.getvalue()
    assert SECRET_KEY not in output
    assert SECRET_WEBHOOK not in output


def test_secret_in_exception_traceback_is_masked() -> None:
    logger, stream = make_logger([SECRET_KEY])
    try:
        raise RuntimeError(f"request failed: key={SECRET_KEY}")
    except RuntimeError:
        logger.exception("エラーが発生しました")
    output = stream.getvalue()
    assert "request failed" in output  # トレースバック自体は出力される
    assert SECRET_KEY not in output
    assert MASK in output


def test_empty_secret_does_not_break_output() -> None:
    logger, stream = make_logger([""])
    logger.info("通常のメッセージ")
    assert "通常のメッセージ" in stream.getvalue()

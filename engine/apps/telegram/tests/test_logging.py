import asyncio
import logging

import httpx
import pytest
from telegram import Bot
from telegram.request import HTTPXRequest

TOKEN = "123456789:not-a-real-token-but-shaped-like-one"


@pytest.mark.parametrize("logger_name", ["httpx", "httpcore"])
def test_http_client_loggers_skip_info(logger_name):
    assert not logging.getLogger(logger_name).isEnabledFor(logging.INFO)


def test_bot_api_calls_do_not_log_the_token(caplog):
    def bot_api(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"ok": True, "result": {"id": 123456789, "is_bot": True, "first_name": "bot", "username": "bot"}}
        )

    # the Bot API URL contains the token: https://api.telegram.org/bot<token>/getMe
    request = HTTPXRequest(httpx_kwargs={"transport": httpx.MockTransport(bot_api)})
    bot = Bot(TOKEN, request=request)

    # INFO is the root level in settings.base LOGGING
    loop = asyncio.new_event_loop()
    try:
        with caplog.at_level(logging.INFO, logger=""):
            loop.run_until_complete(bot.get_me())
    finally:
        loop.close()

    assert TOKEN not in caplog.text

import asyncio
from datetime import datetime
from unittest.mock import patch

import pytest
from asgiref.sync import sync_to_async
from django.db import connections
from telegram import Bot, CallbackQuery, Chat, Message, Update, User

from apps.telegram.client import TelegramClient
from apps.telegram.updates.update_manager import UpdateManager
from engine.management.commands.start_telegram_polling import handle_message


def run_handler(update: Update) -> None:
    """Run handle_message the way python-telegram-bot does: as a coroutine inside a running event loop."""
    # a loop of our own: asyncio.run is patched by nest_asyncio once apps.telegram.utils.run_async has been called
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(handle_message(update, context=None))
    finally:
        # sync_to_async runs thread-sensitive code in a single shared worker thread, close its DB connection
        loop.run_until_complete(sync_to_async(connections.close_all)())
        loop.close()


@pytest.mark.django_db(transaction=True)
def test_handle_message_processes_update_outside_event_loop(
    monkeypatch, make_organization, make_user_for_organization, make_telegram_user_connector
):
    # the ORM must work without the escape hatch
    monkeypatch.delenv("DJANGO_ALLOW_ASYNC_UNSAFE", raising=False)

    organization = make_organization()
    user = make_user_for_organization(organization)
    connector = make_telegram_user_connector(user, telegram_chat_id=123, telegram_nick_name="old-nick")

    update = Update(
        update_id=1,
        message=Message(
            message_id=1,
            date=datetime.now(),
            chat=Chat(id=123, type=Chat.PRIVATE),
            from_user=User(id=123, first_name="Test", is_bot=False, username="new-nick"),
            text="hello",
        ),
    )

    # UpdateManager writes the user's nick name to the DB, which raised SynchronousOnlyOperation in the event loop
    run_handler(update)

    connector.refresh_from_db()
    assert connector.telegram_nick_name == "new-nick"


@pytest.mark.django_db(transaction=True)
def test_handle_message_rebinds_update_to_its_own_bot():
    application_bot = Bot(TelegramClient().token)
    user = User(id=123, first_name="Test", is_bot=False)
    update = Update(
        update_id=1,
        callback_query=CallbackQuery(id="1", from_user=user, chat_instance="1", data="1:0"),
    )
    update.set_bot(application_bot)
    update.callback_query.set_bot(application_bot)

    with patch.object(UpdateManager, "process_update") as mock_process_update:
        run_handler(update)

    processed_update = mock_process_update.call_args.args[0]
    assert processed_update == update
    assert processed_update.callback_query.data == update.callback_query.data
    # callback_query.answer() must not use the Application's bot, whose HTTP client belongs to the polling loop
    assert processed_update.callback_query.get_bot() is not application_bot


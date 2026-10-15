import logging

import telegram.error
from asgiref.sync import sync_to_async
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, MessageHandler, filters

from apps.telegram.client import TelegramClient
from apps.telegram.updates.update_manager import UpdateManager

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


def start_telegram_polling():
    telegram_client = TelegramClient()

    telegram_client.delete_webhook()

    application = Application.builder().token(telegram_client.token).build()

    application.add_error_handler(error_handler)
    application.add_handler(MessageHandler(filters.TEXT, handle_message))
    application.add_handler(CallbackQueryHandler(handle_message))

    # start the long polling loop; blocks until interrupted
    application.run_polling()


async def error_handler(update, context):
    try:
        raise context.error
    except telegram.error.Conflict as e:
        logger.warning(f"Tried to getUpdates() using telegram long polling, but conflict exists, got error: {e}")


async def handle_message(update, context):
    logger.debug(f"Update from Telegram: {update}")

    # python-telegram-bot calls handlers inside its event loop, where the synchronous Django ORM used by
    # UpdateManager raises SynchronousOnlyOperation. Process the update in a worker thread instead.
    await sync_to_async(process_update)(update)


def process_update(update: Update) -> None:
    # The update is bound to the Application's bot, whose HTTP client belongs to the polling event loop.
    # Rebind it to a bot of its own, as the webhook view does, so handlers calling the Bot API from this thread
    # (e.g. callback_query.answer) don't use that client.
    update = Update.de_json(update.to_dict(), bot=TelegramClient().api_client)

    # long-running process outside the request/response cycle: drop stale DB connections, as Django does
    # around each request
    close_old_connections()
    try:
        UpdateManager.process_update(update)
    finally:
        close_old_connections()


class Command(BaseCommand):
    def handle(self, *args, **options):
        logger.info("Starting telegram polling...")
        start_telegram_polling()

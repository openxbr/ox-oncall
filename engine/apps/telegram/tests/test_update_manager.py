from datetime import datetime
from unittest.mock import patch

import pytest
from telegram import Chat, Message, MessageOriginChannel, Update, User

from apps.telegram.updates.update_handlers.channel_to_group_forward import ChannelToGroupForwardHandler
from apps.telegram.updates.update_manager import TELEGRAM_ID, UpdateManager


def generate_discussion_group_update(channel_chat_id, discussion_group_chat_id, edited: bool) -> Update:
    """Message that Telegram auto-forwards from the channel to its discussion group (or an edit of it)."""
    message = Message(
        message_id=2,
        date=datetime.now(),
        chat=Chat(id=discussion_group_chat_id, type=Chat.SUPERGROUP, title="new group name"),
        from_user=User(id=TELEGRAM_ID, first_name="Telegram", is_bot=False),
        forward_origin=MessageOriginChannel(
            date=datetime.now(),
            chat=Chat(id=channel_chat_id, type=Chat.CHANNEL, title="new channel name"),
            message_id=1,
            author_signature="Grafana OnCall",
        ),
        text="alert group",
    )
    if edited:
        return Update(update_id=1, edited_message=message)
    return Update(update_id=1, message=message)


@pytest.mark.django_db
@pytest.mark.parametrize("edited", [False, True])
def test_process_update_from_discussion_group_updates_names(make_organization, make_telegram_channel, edited):
    organization = make_organization()
    connector = make_telegram_channel(organization)

    update = generate_discussion_group_update(
        channel_chat_id=int(connector.channel_chat_id),
        discussion_group_chat_id=int(connector.discussion_group_chat_id),
        edited=edited,
    )
    # edits arrive with update.message set to None, which used to raise AttributeError
    with patch.object(ChannelToGroupForwardHandler, "process_update") as mock_forward_handler:
        UpdateManager.process_update(update)

    # only the original message gets the log and actions reply, not its edits
    assert mock_forward_handler.called is not edited
    connector.refresh_from_db()
    assert connector.channel_name == "new channel name"
    assert connector.discussion_group_name == "new group name"


@pytest.mark.django_db
def test_process_update_edited_message_without_forward_origin():
    message = Message(
        message_id=2,
        date=datetime.now(),
        chat=Chat(id=-100, type=Chat.SUPERGROUP),
        from_user=User(id=TELEGRAM_ID, first_name="Telegram", is_bot=False),
        text="edited",
    )

    UpdateManager.process_update(Update(update_id=1, edited_message=message))

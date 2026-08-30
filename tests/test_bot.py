import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telegram.error import Conflict

import bot
import storage_bluetooth


@pytest.fixture(autouse=True)
def _clear_pending_state():
    bot._awaiting_play_text.clear()
    bot._awaiting_announce_text.clear()
    bot._awaiting_device_pick.clear()
    bot._awaiting_device_nickname.clear()
    bot._pair_sessions.clear()


def _run(coro):
    return asyncio.run(coro)


def _update(user_id=1, text=None):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id, full_name="Test User"),
        message=SimpleNamespace(text=text, reply_text=AsyncMock()),
    )


def _context(args=None):
    return SimpleNamespace(args=args or [], bot=SimpleNamespace(send_message=AsyncMock()))


def test_is_allowed_allows_everyone_when_allowlist_empty(monkeypatch):
    monkeypatch.setattr(bot, "ALLOWED_USER_IDS", set())
    assert bot._is_allowed(_update(user_id=12345)) is True


def test_is_allowed_allows_owner(monkeypatch):
    monkeypatch.setattr(bot, "ALLOWED_USER_IDS", {999})
    monkeypatch.setattr(bot, "OWNER_USER_ID", 42)
    assert bot._is_allowed(_update(user_id=42)) is True


def test_is_allowed_allows_listed_user(monkeypatch):
    monkeypatch.setattr(bot, "ALLOWED_USER_IDS", {999})
    monkeypatch.setattr(bot, "OWNER_USER_ID", 42)
    assert bot._is_allowed(_update(user_id=999)) is True


def test_is_allowed_denies_unlisted_user(monkeypatch):
    monkeypatch.setattr(bot, "ALLOWED_USER_IDS", {999})
    monkeypatch.setattr(bot, "OWNER_USER_ID", 42)
    assert bot._is_allowed(_update(user_id=1)) is False


def test_deny_notifies_owner_when_different_user(monkeypatch):
    monkeypatch.setattr(bot, "OWNER_USER_ID", 42)
    update = _update(user_id=7)
    context = _context()

    _run(bot._deny(update, context))

    update.message.reply_text.assert_awaited_once()
    context.bot.send_message.assert_awaited_once()
    assert context.bot.send_message.call_args.kwargs["chat_id"] == 42


def test_deny_skips_owner_notification_when_denied_user_is_owner(monkeypatch):
    monkeypatch.setattr(bot, "OWNER_USER_ID", 42)
    update = _update(user_id=42)
    context = _context()

    _run(bot._deny(update, context))

    context.bot.send_message.assert_not_awaited()


def test_do_announce_rejects_empty_text():
    update = _update()
    context = _context()

    _run(bot._do_announce(update, context, "   "))

    update.message.reply_text.assert_awaited_once_with("Nothing to announce.")


def test_do_announce_rejects_too_long_text():
    update = _update()
    context = _context()
    text = "x" * (bot.ANNOUNCE_MAX_LENGTH + 1)

    _run(bot._do_announce(update, context, text))

    reply = update.message.reply_text.call_args.args[0]
    assert "too long" in reply


def test_do_announce_plays_announcement_on_success(monkeypatch):
    monkeypatch.setattr(bot, "synthesize_and_serve", lambda text, lang: "http://host/announce.mp3")
    monkeypatch.setattr(bot.player, "announce", lambda url: None)
    update = _update()
    context = _context()

    _run(bot._do_announce(update, context, "hello"))

    update.message.reply_text.assert_awaited_once_with("Announcing: hello")


def test_route_text_denies_disallowed_user(monkeypatch):
    monkeypatch.setattr(bot, "_is_allowed", lambda update: False)
    deny_mock = AsyncMock()
    monkeypatch.setattr(bot, "_deny", deny_mock)
    update = _update(text="hello")
    context = _context()

    _run(bot._route_text(update, context))

    deny_mock.assert_awaited_once_with(update, context)


def test_route_text_routes_pending_announce_user(monkeypatch):
    monkeypatch.setattr(bot, "_is_allowed", lambda update: True)
    do_announce_mock = AsyncMock()
    monkeypatch.setattr(bot, "_do_announce", do_announce_mock)
    update = _update(user_id=5, text="a message")
    context = _context()
    bot._awaiting_announce_text.add(5)

    _run(bot._route_text(update, context))

    do_announce_mock.assert_awaited_once_with(update, context, "a message")
    assert 5 not in bot._awaiting_announce_text


def test_route_text_routes_pending_play_user(monkeypatch):
    monkeypatch.setattr(bot, "_is_allowed", lambda update: True)
    do_play_mock = AsyncMock()
    monkeypatch.setattr(bot, "_do_play", do_play_mock)
    update = _update(user_id=6, text="a query")
    context = _context()
    bot._awaiting_play_text.add(6)

    _run(bot._route_text(update, context))

    do_play_mock.assert_awaited_once_with(update, context, "a query")
    assert 6 not in bot._awaiting_play_text


def test_route_text_defaults_to_play(monkeypatch):
    monkeypatch.setattr(bot, "_is_allowed", lambda update: True)
    do_play_mock = AsyncMock()
    monkeypatch.setattr(bot, "_do_play", do_play_mock)
    update = _update(user_id=7, text="just a search")
    context = _context()

    _run(bot._route_text(update, context))

    do_play_mock.assert_awaited_once_with(update, context, "just a search")


def test_error_handler_sets_conflict_and_stops_on_conflict(monkeypatch):
    monkeypatch.setattr(bot, "conflict_detected", False)
    application = SimpleNamespace(stop_running=Mock())
    context = SimpleNamespace(error=Conflict("conflict"), application=application)

    _run(bot.error_handler(SimpleNamespace(), context))

    assert bot.conflict_detected is True
    context.application.stop_running.assert_called_once()


def test_error_handler_ignores_generic_exception(monkeypatch):
    monkeypatch.setattr(bot, "conflict_detected", False)
    application = SimpleNamespace(stop_running=Mock())
    context = SimpleNamespace(error=ValueError("boom"), application=application)

    _run(bot.error_handler(SimpleNamespace(), context))

    assert bot.conflict_detected is False
    context.application.stop_running.assert_not_called()


def test_on_motion_skips_unknown_category(monkeypatch):
    synth_mock = AsyncMock()
    monkeypatch.setattr(bot, "synthesize_and_serve", synth_mock)

    _run(bot._on_motion("unknown"))

    synth_mock.assert_not_called()


def test_on_motion_skips_during_quiet_hours(monkeypatch):
    monkeypatch.setattr(bot.phrases, "in_quiet_hours", lambda: True)
    synth_mock = AsyncMock()
    monkeypatch.setattr(bot, "synthesize_and_serve", synth_mock)

    _run(bot._on_motion("person"))

    synth_mock.assert_not_called()


def test_on_motion_announces_known_category(monkeypatch):
    monkeypatch.setattr(bot.phrases, "in_quiet_hours", lambda: False)
    monkeypatch.setattr(bot, "synthesize_and_serve", lambda text, lang: "http://host/announce.mp3")
    announce_mock = Mock()
    monkeypatch.setattr(bot.player, "announce", announce_mock)

    _run(bot._on_motion("person"))

    announce_mock.assert_called_once_with("http://host/announce.mp3")


def test_adddevice_scans_and_lists_devices(monkeypatch):
    monkeypatch.setattr(bot, "_is_allowed", lambda update: True)
    discover = AsyncMock(return_value=[{"mac": "AA:BB:CC:DD:EE:FF", "name": "Pixel 9"}])
    monkeypatch.setattr(bot.presence, "discover_devices", discover)
    update = _update()
    context = _context()

    _run(bot.adddevice(update, context))

    replies = [call.args[0] for call in update.message.reply_text.await_args_list]
    assert any("Scanning for nearby Bluetooth devices" in r for r in replies)
    assert any("0. Pixel 9 (AA:BB:CC:DD:EE:FF)" in r for r in replies)
    assert bot._awaiting_device_pick[1]["devices"] == [
        {"mac": "AA:BB:CC:DD:EE:FF", "name": "Pixel 9"}
    ]


def test_adddevice_filters_known_devices(monkeypatch):
    storage_bluetooth.add_device("AA:BB:CC:DD:EE:FF", "Marta")
    discover = AsyncMock(
        return_value=[
            {"mac": "AA:BB:CC:DD:EE:FF", "name": "Pixel 9"},
            {"mac": "11:22:33:44:55:66", "name": "iPad"},
        ]
    )
    monkeypatch.setattr(bot.presence, "discover_devices", discover)
    update = _update()
    context = _context()

    _run(bot.adddevice(update, context))

    replies = [call.args[0] for call in update.message.reply_text.await_args_list]
    assert not any("Pixel 9" in r for r in replies)
    assert any("0. iPad (11:22:33:44:55:66)" in r for r in replies)
    assert bot._awaiting_device_pick[1]["devices"] == [{"mac": "11:22:33:44:55:66", "name": "iPad"}]


def test_adddevice_no_devices_found(monkeypatch):
    monkeypatch.setattr(bot.presence, "discover_devices", AsyncMock(return_value=[]))
    update = _update()
    context = _context()

    _run(bot.adddevice(update, context))

    replies = [call.args[0] for call in update.message.reply_text.await_args_list]
    assert any("No new devices found" in r for r in replies)
    assert 1 not in bot._awaiting_device_pick


def test_finish_device_pick_asks_for_nickname(monkeypatch):
    bot._awaiting_device_pick[1] = {"devices": [{"mac": "AA:BB:CC:DD:EE:FF", "name": "Pixel 9"}]}
    update = _update(text="0")
    context = _context()

    _run(bot._finish_device_pick(update, context))

    reply = update.message.reply_text.call_args.args[0]
    assert "What nickname should I use for Pixel 9?" in reply
    assert bot._awaiting_device_nickname[1] == {"mac": "AA:BB:CC:DD:EE:FF", "name": "Pixel 9"}


def test_finish_device_pick_rejects_bad_index(monkeypatch):
    bot._awaiting_device_pick[1] = {"devices": [{"mac": "AA:BB:CC:DD:EE:FF", "name": "Pixel 9"}]}
    update = _update(text="5")
    context = _context()

    _run(bot._finish_device_pick(update, context))

    reply = update.message.reply_text.call_args.args[0]
    assert "not a valid number" in reply
    assert 1 not in bot._awaiting_device_nickname


def test_save_nickname_pairs_and_stores(monkeypatch):
    monkeypatch.setattr(
        bot.presence, "pair_device", AsyncMock(return_value=(True, "Paired and trusted"))
    )
    bot._awaiting_device_nickname[1] = {"mac": "AA:BB:CC:DD:EE:FF", "name": "Pixel 9"}
    update = _update(text="Marta")
    context = _context()

    _run(bot._save_nickname(update, context))

    reply = update.message.reply_text.call_args.args[0]
    assert "Added Marta (AA:BB:CC:DD:EE:FF)" in reply
    assert storage_bluetooth.list_devices()[0]["nickname"] == "Marta"


def test_save_nickname_rejects_empty(monkeypatch):
    bot._awaiting_device_nickname[1] = {"mac": "AA:BB:CC:DD:EE:FF", "name": "Pixel 9"}
    update = _update(text="   ")
    context = _context()

    _run(bot._save_nickname(update, context))

    reply = update.message.reply_text.call_args.args[0]
    assert "can't be empty" in reply
    assert storage_bluetooth.list_devices() == []


def test_answer_pair_confirm_yes_sets_result():
    async def scenario():
        future = asyncio.get_running_loop().create_future()
        bot._pair_sessions[1] = future
        await bot._answer_pair_confirm(_update(text="yes"), _context())
        return future.result()

    assert _run(scenario()) == "yes"


def test_answer_pair_confirm_no_sets_none():
    async def scenario():
        future = asyncio.get_running_loop().create_future()
        bot._pair_sessions[1] = future
        await bot._answer_pair_confirm(_update(text="no"), _context())
        return future.result()

    assert _run(scenario()) is None


def test_route_text_routes_device_pick(monkeypatch):
    monkeypatch.setattr(bot, "_is_allowed", lambda update: True)
    finish_mock = AsyncMock()
    monkeypatch.setattr(bot, "_finish_device_pick", finish_mock)
    bot._awaiting_device_pick[5] = {"devices": [{"mac": "AA:BB:CC:DD:EE:FF", "name": "Pixel 9"}]}
    update = _update(user_id=5, text="0")
    context = _context()

    _run(bot._route_text(update, context))

    finish_mock.assert_awaited_once_with(update, context)


def test_route_text_routes_device_nickname(monkeypatch):
    monkeypatch.setattr(bot, "_is_allowed", lambda update: True)
    save_mock = AsyncMock()
    monkeypatch.setattr(bot, "_save_nickname", save_mock)
    bot._awaiting_device_nickname[5] = {"mac": "AA:BB:CC:DD:EE:FF", "name": "Pixel 9"}
    update = _update(user_id=5, text="Marta")
    context = _context()

    _run(bot._route_text(update, context))

    save_mock.assert_awaited_once_with(update, context)


def test_route_text_routes_pair_confirm(monkeypatch):
    monkeypatch.setattr(bot, "_is_allowed", lambda update: True)

    async def scenario():
        future = asyncio.get_running_loop().create_future()
        bot._pair_sessions[5] = future
        await bot._route_text(_update(user_id=5, text="yes"), _context())
        return future.result()

    assert _run(scenario()) == "yes"


def test_athome_lists_devices(monkeypatch):
    monkeypatch.setattr(bot, "_is_allowed", lambda update: True)
    storage_bluetooth.add_device("AA:BB:CC:DD:EE:FF", "Marta")
    storage_bluetooth.set_device_state("AA:BB:CC:DD:EE:FF", True, 0, 0.0)
    storage_bluetooth.add_device("11:22:33:44:55:66", "Bob")
    update = _update()
    context = _context()

    _run(bot.athome(update, context))

    reply = update.message.reply_text.call_args.args[0]
    assert "Marta: home" in reply
    assert "AA:BB:CC:DD:EE:FF: home" in reply
    assert "Bob: away" in reply
    assert "11:22:33:44:55:66: away (never seen)" in reply


def test_athome_groups_multiple_devices_per_nickname(monkeypatch):
    monkeypatch.setattr(bot, "_is_allowed", lambda update: True)
    storage_bluetooth.add_device("AA:BB:CC:DD:EE:FF", "gionn")
    storage_bluetooth.set_device_state("AA:BB:CC:DD:EE:FF", True, 0, 0.0)
    storage_bluetooth.add_device("11:22:33:44:55:66", "gionn")
    update = _update()
    context = _context()

    _run(bot.athome(update, context))

    reply = update.message.reply_text.call_args.args[0]
    assert reply.count("gionn: home") == 1
    assert "AA:BB:CC:DD:EE:FF: home" in reply
    assert "11:22:33:44:55:66: away (never seen)" in reply


def test_athome_with_no_devices(monkeypatch):
    monkeypatch.setattr(bot, "_is_allowed", lambda update: True)
    update = _update()
    context = _context()

    _run(bot.athome(update, context))

    reply = update.message.reply_text.call_args.args[0]
    assert "No devices registered" in reply


def test_rmdevice_removes_by_nickname(monkeypatch):
    monkeypatch.setattr(bot, "_is_allowed", lambda update: True)
    storage_bluetooth.add_device("AA:BB:CC:DD:EE:FF", "Marta")
    update = _update()
    context = _context(args=["Marta"])

    _run(bot.rmdevice(update, context))

    reply = update.message.reply_text.call_args.args[0]
    assert "Removed Marta (AA:BB:CC:DD:EE:FF)" in reply
    assert storage_bluetooth.list_devices() == []


def test_rmdevice_removes_by_mac(monkeypatch):
    monkeypatch.setattr(bot, "_is_allowed", lambda update: True)
    storage_bluetooth.add_device("AA:BB:CC:DD:EE:FF", "Marta")
    update = _update()
    context = _context(args=["aa:bb:cc:dd:ee:ff"])

    _run(bot.rmdevice(update, context))

    reply = update.message.reply_text.call_args.args[0]
    assert "Removed Marta (AA:BB:CC:DD:EE:FF)" in reply


def test_rmdevice_not_found(monkeypatch):
    monkeypatch.setattr(bot, "_is_allowed", lambda update: True)
    update = _update()
    context = _context(args=["Nobody"])

    _run(bot.rmdevice(update, context))

    reply = update.message.reply_text.call_args.args[0]
    assert "No device matching" in reply


def test_rmdevice_requires_argument(monkeypatch):
    monkeypatch.setattr(bot, "_is_allowed", lambda update: True)
    update = _update()
    context = _context()

    _run(bot.rmdevice(update, context))

    reply = update.message.reply_text.call_args.args[0]
    assert "Usage:" in reply


def test_on_presence_transition_notifies_owner(monkeypatch):
    send_mock = AsyncMock()
    monkeypatch.setattr(bot, "_bot", SimpleNamespace(send_message=send_mock))
    monkeypatch.setattr(bot, "OWNER_USER_ID", 42)

    _run(
        bot._on_presence_transition({"nickname": "Marta", "mac": "AA:BB:CC:DD:EE:FF", "home": True})
    )

    send_mock.assert_awaited_once_with(chat_id=42, text="Marta is now home.")


def test_on_presence_transition_notifies_owner_away(monkeypatch):
    send_mock = AsyncMock()
    monkeypatch.setattr(bot, "_bot", SimpleNamespace(send_message=send_mock))
    monkeypatch.setattr(bot, "OWNER_USER_ID", 42)

    _run(
        bot._on_presence_transition({"nickname": "Bob", "mac": "AA:BB:CC:DD:EE:FF", "home": False})
    )

    send_mock.assert_awaited_once_with(chat_id=42, text="Bob is now away.")

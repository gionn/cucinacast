import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telegram.error import Conflict

import bot


@pytest.fixture(autouse=True)
def _clear_pending_state():
    bot._awaiting_play_text.clear()
    bot._awaiting_announce_text.clear()
    bot._choices.clear()


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
    serve_mock = Mock()
    monkeypatch.setattr(bot, "serve_sound", serve_mock)

    _run(bot._on_motion("unknown"))

    serve_mock.assert_not_called()


def test_on_motion_skips_during_quiet_hours(monkeypatch):
    monkeypatch.setattr(bot.phrases, "in_quiet_hours", lambda: True)
    serve_mock = Mock()
    monkeypatch.setattr(bot, "serve_sound", serve_mock)

    _run(bot._on_motion("person"))

    serve_mock.assert_not_called()


def test_on_motion_plays_category_sound(monkeypatch):
    monkeypatch.setattr(bot.phrases, "in_quiet_hours", lambda: False)
    serve_mock = Mock(return_value="http://host/motion.mp3")
    monkeypatch.setattr(bot, "serve_sound", serve_mock)
    announce_mock = Mock()
    monkeypatch.setattr(bot.player, "announce", announce_mock)

    _run(bot._on_motion("person"))

    serve_mock.assert_called_once_with(bot.sounds.sound_path("person"))
    announce_mock.assert_called_once_with("http://host/motion.mp3")


def test_route_text_cancel_falls_through_outside_device_flow(monkeypatch):
    monkeypatch.setattr(bot, "_is_allowed", lambda update: True)
    do_play_mock = AsyncMock()
    monkeypatch.setattr(bot, "_do_play", do_play_mock)
    update = _update(user_id=5, text="cancel")
    context = _context()

    _run(bot._route_text(update, context))

    do_play_mock.assert_awaited_once_with(update, context, "cancel")


def _callback_update(user_id=1, data="choice:abc"):
    query = SimpleNamespace(
        data=data,
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id, full_name="Test User"),
        callback_query=query,
    )


def _navidrome_result(albums=(), songs=()):
    return {"albums": list(albums), "songs": list(songs)}


def _album(id="a1", name="Discovery", artist="Daft Punk"):
    return {"id": id, "name": name, "artist": artist}


def _song(id="s1", title="One More Time", artist="Daft Punk", album="Discovery"):
    return {
        "id": id,
        "title": title,
        "artist": artist,
        "album": album,
        "album_id": "a1",
    }


def test_do_play_skips_navidrome_when_not_configured(monkeypatch):
    monkeypatch.setattr(bot.navidrome, "enabled", lambda: False)
    search = Mock()
    monkeypatch.setattr(bot.navidrome, "search", search)
    play_mock = Mock(return_value=("Track", "https://youtube/watch"))
    monkeypatch.setattr(bot.player, "play_search", play_mock)
    update = _update()
    context = _context()

    _run(bot._do_play(update, context, "daft punk"))

    search.assert_not_called()
    play_mock.assert_called_once_with("daft punk")
    assert update.message.reply_text.await_args_list[-1].args[0] == (
        "Now playing: Track\nhttps://youtube/watch"
    )


def test_do_play_offers_albums_and_songs_from_navidrome(monkeypatch):
    monkeypatch.setattr(bot.navidrome, "enabled", lambda: True)
    monkeypatch.setattr(
        bot.navidrome,
        "search",
        lambda query, **kwargs: _navidrome_result(albums=[_album()], songs=[_song()]),
    )
    play_mock = Mock()
    monkeypatch.setattr(bot.player, "play_search", play_mock)
    update = _update()
    context = _context()

    _run(bot._do_play(update, context, "daft punk"))

    play_mock.assert_not_called()
    reply = update.message.reply_text.await_args_list[-1]
    assert "Found on Navidrome" in reply.args[0]
    rows = reply.kwargs["reply_markup"].inline_keyboard
    assert [row[0].text for row in rows] == [
        "Discovery — Daft Punk",
        "♪ One More Time — Daft Punk",
        "Search YouTube",
    ]
    assert all(row[0].callback_data.startswith("choice:") for row in rows)
    assert [choice["kind"] for choice in bot._choices.values()] == [
        "album",
        "song",
        "youtube",
    ]


def test_do_play_falls_back_silently_when_navidrome_has_no_match(monkeypatch):
    monkeypatch.setattr(bot.navidrome, "enabled", lambda: True)
    monkeypatch.setattr(
        bot.navidrome,
        "search",
        lambda query, **kwargs: _navidrome_result(),
    )
    play_mock = Mock(return_value=("Track", "https://youtube/watch"))
    monkeypatch.setattr(bot.player, "play_search", play_mock)
    update = _update()
    context = _context()

    _run(bot._do_play(update, context, "daft punk"))

    play_mock.assert_called_once_with("daft punk")
    assert "Now playing: Track" in update.message.reply_text.await_args_list[-1].args[0]


def test_do_play_falls_back_when_navidrome_is_unavailable(monkeypatch):
    monkeypatch.setattr(bot.navidrome, "enabled", lambda: True)

    def raise_unavailable(query, **kwargs):
        raise bot.navidrome.NavidromeUnavailable("down")

    monkeypatch.setattr(bot.navidrome, "search", raise_unavailable)
    play_mock = Mock(return_value=("Track", "https://youtube/watch"))
    monkeypatch.setattr(bot.player, "play_search", play_mock)
    update = _update()
    context = _context()

    _run(bot._do_play(update, context, "daft punk"))

    play_mock.assert_called_once_with("daft punk")


def test_remember_choice_drops_the_oldest_entries(monkeypatch):
    monkeypatch.setattr(bot, "MAX_PENDING_CHOICES", 2)

    first = bot._remember_choice({"kind": "youtube"})
    second = bot._remember_choice({"kind": "youtube"})
    third = bot._remember_choice({"kind": "youtube"})

    assert first not in bot._choices
    assert second in bot._choices
    assert third in bot._choices


def test_on_choice_plays_the_selected_album(monkeypatch):
    token = bot._remember_choice({"kind": "album", "album": _album(), "query": "daft punk"})
    update = _callback_update(data=f"choice:{token}")
    context = _context()
    entries = [{"id": "s1", "title": "One More Time", "url": "http://nav/1"}]
    album = {**_album(), "entries": entries}
    monkeypatch.setattr(bot.navidrome, "album_entries", lambda album_id: album)
    play_mock = Mock(return_value=("One More Time", "http://nav/1"))
    monkeypatch.setattr(bot.player, "play_entries", play_mock)

    _run(bot._on_choice(update, context))

    play_mock.assert_called_once_with(entries, "navidrome", "Discovery")
    last_edit = update.callback_query.edit_message_text.await_args_list[-1]
    assert "Now playing album" in last_edit.args[0]


def test_on_choice_plays_the_selected_song(monkeypatch):
    token = bot._remember_choice({"kind": "song", "song": _song(), "query": "daft punk"})
    update = _callback_update(data=f"choice:{token}")
    context = _context()
    entry = {"id": "s1", "title": "One More Time", "url": "http://nav/1"}
    monkeypatch.setattr(bot.navidrome, "song_entry", lambda song_id: entry)
    play_mock = Mock(return_value=("One More Time", "http://nav/1"))
    monkeypatch.setattr(bot.player, "play_entries", play_mock)

    _run(bot._on_choice(update, context))

    play_mock.assert_called_once_with([entry], "navidrome", "One More Time")
    last_edit = update.callback_query.edit_message_text.await_args_list[-1]
    assert "Now playing: One More Time" in last_edit.args[0]


def test_on_choice_searches_youtube(monkeypatch):
    token = bot._remember_choice({"kind": "youtube", "query": "daft punk"})
    update = _callback_update(data=f"choice:{token}")
    context = _context()
    play_mock = Mock(return_value=("Track", "https://youtube/watch"))
    monkeypatch.setattr(bot.player, "play_search", play_mock)

    _run(bot._on_choice(update, context))

    play_mock.assert_called_once_with("daft punk")
    last_edit = update.callback_query.edit_message_text.await_args_list[-1]
    assert "Now playing: Track" in last_edit.args[0]


def test_on_choice_reports_an_expired_selection(monkeypatch):
    update = _callback_update(data="choice:missing")
    context = _context()
    play_mock = Mock()
    monkeypatch.setattr(bot.player, "play_search", play_mock)

    _run(bot._on_choice(update, context))

    update.callback_query.edit_message_text.assert_awaited_once_with(
        "That selection expired. Send /play again."
    )
    play_mock.assert_not_called()


def test_on_choice_denies_a_disallowed_user(monkeypatch):
    token = bot._remember_choice({"kind": "youtube", "query": "daft punk"})
    update = _callback_update(user_id=7, data=f"choice:{token}")
    context = _context()
    monkeypatch.setattr(bot, "_is_allowed", lambda update: False)
    play_mock = Mock()
    monkeypatch.setattr(bot.player, "play_search", play_mock)

    _run(bot._on_choice(update, context))

    update.callback_query.answer.assert_awaited_once()
    context.bot.send_message.assert_awaited_once()
    play_mock.assert_not_called()


def test_nowplaying_omits_a_hidden_url(monkeypatch):
    monkeypatch.setattr(bot, "_is_allowed", lambda update: True)
    monkeypatch.setattr(bot.player, "now_playing", lambda: ("Track", None, "PLAYING"))
    update = _update()
    context = _context()

    _run(bot.nowplaying(update, context))

    update.message.reply_text.assert_awaited_once_with("Now playing (PLAYING): Track")


def test_skip_omits_a_hidden_url(monkeypatch):
    monkeypatch.setattr(bot, "_is_allowed", lambda update: True)
    monkeypatch.setattr(bot.player, "skip", lambda: ("Track", None))
    update = _update()
    context = _context()

    _run(bot.skip(update, context))

    update.message.reply_text.assert_awaited_once_with("Now playing: Track")

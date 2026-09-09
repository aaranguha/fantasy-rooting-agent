"""Notification providers.  No real message is ever sent from the test suite -
iMessage in particular is exercised only through a stubbed osascript."""

from __future__ import annotations

import io
import subprocess

import pytest

from app.notifiers import PRIORITY, autoselect, available_notifiers, build
from app.notifiers.base import NotificationError
from app.notifiers.console import ConsoleNotifier
from app.notifiers.imessage import IMessageNotifier
from app.notifiers.ntfy import NtfyNotifier
from app.notifiers.telegram import TelegramNotifier
from app.notifiers.twilio import TwilioNotifier


def test_priority_order_is_free_first_and_twilio_last():
    assert PRIORITY == ["imessage", "ntfy", "telegram", "console", "twilio"]


def test_console_notifier_always_works_and_captures_output():
    buf = io.StringIO()
    n = ConsoleNotifier(stream=buf)
    res = n.send("TITLE", "body text")
    assert res.ok and "TITLE" in buf.getvalue() and "body text" in buf.getvalue()


def test_dry_run_sends_nothing():
    buf = io.StringIO()
    n = ConsoleNotifier(stream=buf, dry_run=True)
    res = n.send("TITLE", "body")
    assert res.ok and res.dry_run and n.sent == []
    assert buf.getvalue() == ""


def test_unconfigured_provider_reports_why_instead_of_raising(monkeypatch):
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    res = NtfyNotifier().send("t", "b")
    assert not res.ok and "NTFY_TOPIC" in res.detail


def test_ntfy_posts_body_as_utf8_and_strips_emoji_from_the_header(monkeypatch):
    captured = {}

    class FakeResp:
        status_code = 200
        text = "ok"

    def fake_post(url, data=None, headers=None, auth=None, timeout=None):
        captured.update(url=url, data=data, headers=headers, auth=auth)
        return FakeResp()

    monkeypatch.setattr("app.notifiers.ntfy.requests.post", fake_post)
    n = NtfyNotifier(server="https://ntfy.sh", topic="secret-topic")
    res = n.send("🏈 SNF in 15: DAL @ PHI", "🔥 body with emoji")

    assert res.ok
    assert captured["url"] == "https://ntfy.sh/secret-topic"
    assert captured["data"] == "🔥 body with emoji".encode("utf-8")
    captured["headers"]["Title"].encode("latin-1")     # must not raise
    assert "SNF in 15" in captured["headers"]["Title"]
    assert "secret-topic" not in res.detail, "the topic is a secret; keep it out of logs"


def test_ntfy_supports_a_bearer_token_for_private_servers(monkeypatch):
    captured = {}
    monkeypatch.setattr("app.notifiers.ntfy.requests.post",
                        lambda url, **kw: (captured.update(kw), type("R", (), {"status_code": 200, "text": ""}))[1])
    NtfyNotifier(topic="t", token="tk_secret").send("a", "b")
    assert captured["headers"]["Authorization"] == "Bearer tk_secret"


def test_ntfy_server_error_is_retried_then_reported(monkeypatch):
    monkeypatch.setattr("app.notifiers.base.time.sleep", lambda *_: None)
    calls = []

    def fail(url, **kw):
        calls.append(url)
        return type("R", (), {"status_code": 500, "text": "boom"})()

    monkeypatch.setattr("app.notifiers.ntfy.requests.post", fail)
    res = NtfyNotifier(topic="t").send("a", "b")
    assert not res.ok and len(calls) == 3 and "500" in res.detail


def test_telegram_sends_title_and_body_together(monkeypatch):
    captured = {}

    class FakeResp:
        status_code = 200
        text = ""
        def json(self): return {"ok": True, "result": {"message_id": 555}}

    monkeypatch.setattr(
        "app.notifiers.telegram.requests.post",
        lambda url, json=None, timeout=None: (captured.update(url=url, json=json), FakeResp())[1])
    res = TelegramNotifier(token="123:abc", chat_id="999").send("TITLE", "BODY")
    assert res.ok
    assert res.message_id == "555"
    assert "bot123:abc/sendMessage" in captured["url"]
    assert captured["json"]["chat_id"] == "999"
    assert captured["json"]["text"] == "TITLE\n\nBODY"


def test_telegram_delete_calls_deleteMessage(monkeypatch):
    captured = {}

    class FakeResp:
        status_code = 200
        def json(self): return {"ok": True}

    monkeypatch.setattr(
        "app.notifiers.telegram.requests.post",
        lambda url, json=None, timeout=None: (captured.update(url=url, json=json), FakeResp())[1])
    n = TelegramNotifier(token="123:abc", chat_id="999")
    assert n.delete("555") is True
    assert "deleteMessage" in captured["url"]
    assert captured["json"] == {"chat_id": "999", "message_id": 555}


def test_telegram_delete_of_an_empty_id_is_a_harmless_no_op():
    assert TelegramNotifier(token="123:abc", chat_id="999").delete("") is False


def test_telegram_delete_failure_is_swallowed_not_raised(monkeypatch):
    class FakeResp:
        status_code = 400
        def json(self): return {"ok": False}

    monkeypatch.setattr("app.notifiers.telegram.requests.post",
                        lambda *a, **k: FakeResp())
    assert TelegramNotifier(token="123:abc", chat_id="999").delete("555") is False


def test_console_notifier_delete_is_a_harmless_no_op():
    assert ConsoleNotifier().delete("anything") is False


def test_telegram_missing_chat_id_is_a_clear_message():
    res = TelegramNotifier(token="123:abc", chat_id="").send("a", "b")
    assert not res.ok and "TELEGRAM_CHAT_ID" in res.detail


# ---------------------------------------------------------------------------
# iMessage - stubbed, never actually sends
# ---------------------------------------------------------------------------

def test_imessage_requires_macos_and_a_recipient(monkeypatch):
    monkeypatch.setattr("app.notifiers.imessage.platform.system", lambda: "Linux")
    ok, why = IMessageNotifier(to="+15551234567").available()
    assert not ok and "macOS" in why

    monkeypatch.setattr("app.notifiers.imessage.platform.system", lambda: "Darwin")
    monkeypatch.setattr("app.notifiers.imessage.shutil.which", lambda _: "/usr/bin/osascript")
    ok, why = IMessageNotifier(to="").available()
    assert not ok and "IMESSAGE_TO" in why


def test_imessage_builds_valid_applescript_without_sending(monkeypatch):
    scripts = []

    def fake_run(cmd, **kw):
        if cmd[:2] == ["osascript", "-"]:
            scripts.append(kw.get("input", ""))
            return subprocess.CompletedProcess(cmd, 0, "sent via iMessage", "")
        return subprocess.CompletedProcess(cmd, 0, "true", "")

    monkeypatch.setattr("app.notifiers.imessage.platform.system", lambda: "Darwin")
    monkeypatch.setattr("app.notifiers.imessage.shutil.which", lambda _: "/usr/bin/osascript")
    monkeypatch.setattr("app.notifiers.imessage.subprocess.run", fake_run)

    res = IMessageNotifier(to="+15551234567").send('TITLE "quoted"', "body\nline")
    assert res.ok and res.detail == "sent via iMessage"
    assert len(scripts) == 1
    assert '"+15551234567"' in scripts[0]
    assert 'service type = iMessage' in scripts[0]
    assert '\\"quoted\\"' in scripts[0], "quotes must be escaped for AppleScript"


def test_imessage_falls_back_to_the_sms_dialect(monkeypatch):
    attempts = []

    def fake_run(cmd, **kw):
        if cmd[:2] != ["osascript", "-"]:
            return subprocess.CompletedProcess(cmd, 0, "true", "")
        script = kw.get("input", "")
        attempts.append(script)
        if "service type = iMessage" in script:
            return subprocess.CompletedProcess(cmd, 1, "", "error: can't get participant")
        return subprocess.CompletedProcess(cmd, 0, "sent via SMS relay", "")

    monkeypatch.setattr("app.notifiers.imessage.platform.system", lambda: "Darwin")
    monkeypatch.setattr("app.notifiers.imessage.shutil.which", lambda _: "/usr/bin/osascript")
    monkeypatch.setattr("app.notifiers.imessage.subprocess.run", fake_run)

    res = IMessageNotifier(to="+15551234567").send("T", "B")
    assert res.ok and "SMS" in res.detail and len(attempts) == 2


def test_imessage_automation_denial_gives_setup_instructions(monkeypatch):
    def fake_run(cmd, **kw):
        if cmd[:2] != ["osascript", "-"]:
            return subprocess.CompletedProcess(cmd, 0, "true", "")
        return subprocess.CompletedProcess(cmd, 1, "", "execution error: Not authorized (-1743)")

    monkeypatch.setattr("app.notifiers.base.time.sleep", lambda *_: None)
    monkeypatch.setattr("app.notifiers.imessage.platform.system", lambda: "Darwin")
    monkeypatch.setattr("app.notifiers.imessage.shutil.which", lambda _: "/usr/bin/osascript")
    monkeypatch.setattr("app.notifiers.imessage.subprocess.run", fake_run)

    res = IMessageNotifier(to="+1555").send("T", "B")
    assert not res.ok
    assert "Automation" in res.detail and "Privacy & Security" in res.detail


def test_imessage_is_flagged_as_needing_a_local_mac():
    assert IMessageNotifier(to="x").requires_local_mac is True
    assert NtfyNotifier(topic="x").requires_local_mac is False


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def test_twilio_is_never_auto_selected(monkeypatch):
    for var in ("IMESSAGE_TO", "NTFY_TOPIC", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(var, raising=False)
    for var, val in (("TWILIO_ACCOUNT_SID", "a"), ("TWILIO_AUTH_TOKEN", "b"),
                     ("TWILIO_FROM", "c"), ("TWILIO_TO", "d")):
        monkeypatch.setenv(var, val)
    assert autoselect(None).name == "console"


def test_autoselect_falls_back_when_the_preferred_provider_is_unconfigured(monkeypatch):
    for var in ("IMESSAGE_TO", "NTFY_TOPIC", "TELEGRAM_BOT_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    assert autoselect("ntfy").name == "console"
    monkeypatch.setenv("NTFY_TOPIC", "abc")
    assert autoselect("ntfy").name == "ntfy"


def test_unknown_provider_name_is_a_helpful_error():
    with pytest.raises(NotificationError) as exc:
        build("carrier-pigeon")
    assert "imessage" in str(exc.value)


def test_available_notifiers_lists_every_provider():
    assert [n for n, _, _ in available_notifiers()] == PRIORITY


def test_twilio_is_optional_and_absent_by_default(monkeypatch):
    for var in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM", "TWILIO_TO"):
        monkeypatch.delenv(var, raising=False)
    ok, why = TwilioNotifier().available()
    assert not ok and "TWILIO_ACCOUNT_SID" in why


def test_telegram_rejects_the_bots_own_id_as_the_chat_id():
    """Telegram answers this case with a confusing 403 'the bot can't send
    messages to the bot'. Catch it before the send and say what's actually wrong."""
    n = TelegramNotifier(token="8630784085:AAH-secret", chat_id="8630784085")
    ok, why = n.available()
    assert not ok
    assert "BOT's own id" in why and "telegram-chat-id" in why

    res = n.send("t", "b")
    assert not res.ok and "cannot message itself" in res.detail


def test_telegram_accepts_a_real_personal_chat_id():
    n = TelegramNotifier(token="8630784085:AAH-secret", chat_id="123456789")
    assert n.available()[0]
    assert n.bot_id == "8630784085"


def test_discover_chat_id_filters_out_bots(monkeypatch):
    payload = {"result": [
        {"message": {"chat": {"id": 8630784085, "first_name": "Fantasy Agent",
                              "type": "private"},
                     "from": {"is_bot": True}}},
        {"message": {"chat": {"id": 123456789, "first_name": "Aaran",
                              "type": "private"},
                     "from": {"is_bot": False}}},
        {"message": {"chat": {"id": 123456789, "first_name": "Aaran",
                              "type": "private"},
                     "from": {"is_bot": False}}},
    ]}

    class R:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return payload

    monkeypatch.setattr("app.notifiers.telegram.requests.get", lambda *a, **k: R())
    chats = TelegramNotifier(token="8630784085:AAH", chat_id="").discover_chat_id()
    assert [c["id"] for c in chats] == ["123456789"], "bots and duplicates must be dropped"
    assert chats[0]["name"] == "Aaran"


def test_get_me_identifies_which_bot_a_token_belongs_to(monkeypatch):
    class R:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"ok": True, "result": {
            "id": 8630784085, "is_bot": True, "first_name": "Fantasy Agent",
            "username": "fantasy_rooting_bot"}}

    monkeypatch.setattr("app.notifiers.telegram.requests.get", lambda *a, **k: R())
    who = TelegramNotifier(token="8630784085:AAH", chat_id="1").me()
    assert who["username"] == "fantasy_rooting_bot"


def test_get_me_reports_a_bad_token_clearly(monkeypatch):
    class R:
        status_code = 401
        def raise_for_status(self): raise AssertionError("should not reach here")
        def json(self): return {}

    monkeypatch.setattr("app.notifiers.telegram.requests.get", lambda *a, **k: R())
    with pytest.raises(NotificationError) as exc:
        TelegramNotifier(token="bad:token", chat_id="1").me()
    assert "401" in str(exc.value)

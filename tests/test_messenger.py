"""
Unit tests for the Messenger platform plugin.

Run from the repo root with the hermes-agent checkout on PYTHONPATH so the
``gateway.*`` imports resolve, e.g.::

    PYTHONPATH=/path/to/hermes-agent python -m pytest tests -q

No network is touched — the Graph API client is monkeypatched.
"""

import hashlib
import hmac
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

# Make the plugin package importable as ``messenger`` regardless of cwd.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from messenger import adapter as mod  # noqa: E402
from gateway.config import Platform, PlatformConfig  # noqa: E402


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------

def _sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_signature_valid():
    body = b'{"object":"page"}'
    secret = "s3cr3t"
    assert mod.verify_messenger_signature(body, _sign(body, secret), secret) is True


def test_signature_tampered_body():
    secret = "s3cr3t"
    sig = _sign(b'{"object":"page"}', secret)
    assert mod.verify_messenger_signature(b'{"object":"x"}', sig, secret) is False


def test_signature_missing_or_malformed():
    assert mod.verify_messenger_signature(b"x", "", "s") is False
    assert mod.verify_messenger_signature(b"x", "md5=abc", "s") is False
    assert mod.verify_messenger_signature(b"x", "sha256=deadbeef", "s") is False


# ---------------------------------------------------------------------------
# Markdown stripping + chunking
# ---------------------------------------------------------------------------

def test_strip_markdown_keeps_urls():
    out = mod.strip_markdown_preserving_urls(
        "**bold** and [docs](https://example.com) and `code`"
    )
    assert "**" not in out
    assert "https://example.com" in out
    assert "docs (https://example.com)" in out


def test_split_never_exceeds_max():
    text = "word " * 2000  # ~10000 chars
    chunks = mod.split_for_messenger(text, max_chars=2000)
    assert chunks
    assert all(len(c) <= 2000 for c in chunks)


def test_split_no_midword_break():
    text = "alpha " * 500
    chunks = mod.split_for_messenger(text, max_chars=100)
    # Reassembled (single-spaced) must not invent or cut tokens.
    for c in chunks:
        for tok in c.split():
            assert tok == "alpha"


def test_split_short_text_single_chunk():
    assert mod.split_for_messenger("hello") == ["hello"]
    assert mod.split_for_messenger("") == []


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------

def test_dedup_drops_repeat():
    d = mod._MessageDeduplicator()
    assert d.is_duplicate("mid.1") is False
    assert d.is_duplicate("mid.1") is True
    assert d.is_duplicate("") is False  # empty never dedups


# ---------------------------------------------------------------------------
# Messenger session identity
# ---------------------------------------------------------------------------

def test_session_thread_id_uses_page_recipient():
    assert mod._messenger_session_thread_id(
        {"sender": {"id": "USER"}, "recipient": {"id": "PAGE"}}
    ) == "PAGE"
    assert mod._messenger_session_thread_id({"sender": {"id": "USER"}}) is None


# ---------------------------------------------------------------------------
# Platform enum resolution (the §7 Q1 blocker)
# ---------------------------------------------------------------------------

def test_platform_resolves_after_register():
    captured = {}

    class _Ctx:
        class manifest:  # noqa: N801
            name = "messenger"

        def register_platform(self, **kwargs):
            captured.update(kwargs)
            # Mirror what PluginContext.register_platform does: register in
            # the platform_registry so Platform("messenger") resolves.
            from gateway.platform_registry import platform_registry, PlatformEntry

            platform_registry.register(
                PlatformEntry(
                    name=kwargs["name"],
                    label=kwargs["label"],
                    adapter_factory=kwargs["adapter_factory"],
                    check_fn=kwargs["check_fn"],
                    source="plugin",
                )
            )

    mod.register(_Ctx())
    assert captured["name"] == "messenger"
    assert "MESSENGER_PAGE_ACCESS_TOKEN" in captured["required_env"]
    # Now the enum must resolve the plugin platform.
    assert Platform("messenger").value == "messenger"
    assert Platform("messenger") is Platform("messenger")


# ---------------------------------------------------------------------------
# Adapter construction + allowlist
# ---------------------------------------------------------------------------

def _make_adapter(monkeypatch, **env):
    base = {
        "MESSENGER_PAGE_ACCESS_TOKEN": "tok",
        "MESSENGER_APP_SECRET": "sec",
        "MESSENGER_VERIFY_TOKEN": "vt",
    }
    base.update(env)
    for k, v in base.items():
        monkeypatch.setenv(k, v)
    # register so Platform("messenger") resolves during __init__
    from gateway.platform_registry import platform_registry, PlatformEntry
    if not platform_registry.is_registered("messenger"):
        platform_registry.register(
            PlatformEntry(
                name="messenger", label="Messenger",
                adapter_factory=lambda c: None, check_fn=lambda: True,
                source="plugin",
            )
        )
    return mod.MessengerAdapter(PlatformConfig(enabled=True))


def test_adapter_init_reads_env(monkeypatch):
    a = _make_adapter(monkeypatch, MESSENGER_PORT="9000", MESSENGER_ALLOWED_USERS="A,B")
    assert a.page_access_token == "tok"
    assert a.app_secret == "sec"
    assert a.verify_token == "vt"
    assert a.webhook_port == 9000
    assert a.allowed_users == {"A", "B"}
    assert a.allow_all is False
    assert a.platform.value == "messenger"


def test_allowlist_via_dispatch(monkeypatch):
    import asyncio

    a = _make_adapter(monkeypatch, MESSENGER_ALLOWED_USERS="GOOD")
    handled = []

    async def _fake_handle(event):
        handled.append(event)

    monkeypatch.setattr(a, "handle_message", _fake_handle)
    # No client → skip profile/typing network; simulate by setting a stub.
    a._client = None

    # Unauthorized sender dropped.
    asyncio.run(a._dispatch_event({"sender": {"id": "BAD"}, "message": {"mid": "m1", "text": "hi"}}))
    assert handled == []

    # Authorized sender passes through to handle_message.
    asyncio.run(a._dispatch_event({
        "sender": {"id": "GOOD"},
        "recipient": {"id": "PAGE"},
        "message": {"mid": "m2", "text": "hi"},
    }))
    assert len(handled) == 1
    ev = handled[0]
    assert ev.text == "hi"
    assert ev.source.chat_id == "GOOD"
    assert ev.source.chat_type == "dm"
    assert ev.source.thread_id == "PAGE"


def test_message_source_uses_facebook_profile_name(monkeypatch):
    import asyncio

    a = _make_adapter(monkeypatch, MESSENGER_ALLOW_ALL_USERS="true")
    handled = []

    class _FakeClient:
        async def send_action(self, *args, **kwargs):
            return None

        async def get_user_profile(self, psid):
            assert psid == "GOOD"
            return {"name": "Facebook User"}

    async def _fake_handle(event):
        handled.append(event)

    monkeypatch.setattr(a, "handle_message", _fake_handle)
    a._client = _FakeClient()

    asyncio.run(a._dispatch_event({
        "sender": {"id": "GOOD"},
        "recipient": {"id": "PAGE"},
        "message": {"mid": "m-profile", "text": "hi"},
    }))

    assert len(handled) == 1
    ev = handled[0]
    assert ev.source.user_id == "GOOD"
    assert ev.source.user_name == "Facebook User"
    assert ev.source.chat_name == "Facebook User"


def test_echo_and_dedup_filtered(monkeypatch):
    import asyncio

    a = _make_adapter(monkeypatch, MESSENGER_ALLOW_ALL_USERS="true")
    a._client = None
    handled = []

    async def _fake_handle(event):
        handled.append(event)

    monkeypatch.setattr(a, "handle_message", _fake_handle)

    # Echo dropped.
    asyncio.run(a._dispatch_event({"sender": {"id": "X"}, "message": {"is_echo": True, "mid": "e1"}}))
    # First real delivered, duplicate dropped.
    asyncio.run(a._dispatch_event({"sender": {"id": "X"}, "message": {"mid": "d1", "text": "a"}}))
    asyncio.run(a._dispatch_event({"sender": {"id": "X"}, "message": {"mid": "d1", "text": "a"}}))
    assert len(handled) == 1


# ---------------------------------------------------------------------------
# send() chunking + error handling (mock client)
# ---------------------------------------------------------------------------

def test_send_chunks_long_reply(monkeypatch):
    import asyncio

    a = _make_adapter(monkeypatch, MESSENGER_ALLOW_ALL_USERS="true")
    calls = []

    class _FakeClient:
        async def send_message(self, psid, message_obj, **kw):
            calls.append(message_obj["text"])
            return {"message_id": "mid.x"}

    a._client = _FakeClient()
    long_text = "word " * 1200  # ~6000 chars → multiple 2000-char chunks
    res = asyncio.run(a.send("PSID", long_text))
    assert res.success is True
    assert len(calls) >= 2
    assert all(len(c) <= 2000 for c in calls)


def test_send_returns_failure_on_error(monkeypatch):
    import asyncio

    a = _make_adapter(monkeypatch, MESSENGER_ALLOW_ALL_USERS="true")

    class _BoomClient:
        async def send_message(self, *a, **k):
            raise RuntimeError("Messenger send 400: bad")

    a._client = _BoomClient()
    res = asyncio.run(a.send("PSID", "hi"))
    assert res.success is False
    assert "400" in (res.error or "")


# ---------------------------------------------------------------------------
# get_chat_info
# ---------------------------------------------------------------------------

def test_get_chat_info_no_client(monkeypatch):
    import asyncio

    a = _make_adapter(monkeypatch, MESSENGER_ALLOW_ALL_USERS="true")
    a._client = None
    info = asyncio.run(a.get_chat_info("PSID"))
    assert info == {"name": "PSID", "type": "dm", "chat_id": "PSID"}

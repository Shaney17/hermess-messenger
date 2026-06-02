"""
Facebook Messenger platform adapter for Hermes Agent.

A user-installable platform plugin (lives in ``~/.hermes/plugins/messenger/``
so ``hermes update`` never clobbers it). Runs an aiohttp webhook server,
answers Meta's GET verification handshake, verifies inbound POST events with
``X-Hub-Signature-256`` (HMAC-SHA256 keyed by the App Secret), and relays 1:1
Page DMs to/from the agent via the standard ``BasePlatformAdapter`` interface.

Design highlights
-----------------

**GET verify handshake.** Meta validates the webhook once by issuing a GET
with ``hub.mode=subscribe``, ``hub.verify_token``, ``hub.challenge``. We echo
the challenge verbatim when the verify token matches.

**Send API, RESPONSE type.** Messenger has no reply token. Outbound replies
POST to ``{graph}/{ver}/me/messages`` with ``messaging_type=RESPONSE``, which
is valid within the 24-hour standard messaging window (the normal
reply-to-inbound flow). Proactive/cron sends outside the window need an
approved message tag — see ``_standalone_send``.

**1:1 only.** The Messenger Platform delivers Page DMs; there is no group
primitive on the channel API. Every chat is ``chat_type="dm"`` keyed by PSID.

**2000-char chunking.** Messenger caps a text message at 2000 chars. Longer
replies are smart-chunked on paragraph/line/word boundaries and sent as
sequential Send API calls (one message object per call).

**At-least-once dedup.** Meta redelivers webhooks; we dedup on ``message.mid``.
Echo events (``message.is_echo``) and Page-sender events are dropped to avoid
reply loops.

Modeled on the bundled LINE plugin (``plugins/platforms/line/adapter.py``),
which shares the webhook + HMAC-verify + allowlist shape.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    cache_image_from_bytes,
    cache_audio_from_bytes,
    cache_video_from_bytes,
    cache_document_from_bytes,
)
from gateway.config import Platform


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GRAPH_API_BASE = "https://graph.facebook.com"
DEFAULT_API_VERSION = "v21.0"  # Verify current stable version before shipping.

# Messenger hard limits
MESSENGER_MAX_CHARS = 2000  # Per text message object.

# Webhook hardening
WEBHOOK_BODY_MAX_BYTES = 1_048_576  # 1 MiB — webhooks are tiny JSON.
DEFAULT_WEBHOOK_PORT = 8650
DEFAULT_WEBHOOK_PATH = "/messenger/webhook"

# Inbound media handling
MEDIA_FETCH_TIMEOUT = 30.0
SEND_TIMEOUT = 15.0


# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------

def _csv_set(value: str) -> Set[str]:
    """Parse a comma-separated env value into a set of trimmed tokens."""
    if not value:
        return set()
    return {tok.strip() for tok in value.split(",") if tok.strip()}


def _truthy_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Webhook signature verification
# ---------------------------------------------------------------------------

def verify_messenger_signature(body: bytes, header: str, app_secret: str) -> bool:
    """Verify a Messenger webhook's ``X-Hub-Signature-256`` header.

    Meta signs the *raw* request body with HMAC-SHA256 keyed by the App
    Secret and sends it as ``sha256=<hexdigest>``. Constant-time comparison
    defends against timing oracles.
    """
    if not header or not app_secret or body is None:
        return False
    if not header.startswith("sha256="):
        return False
    provided = header.split("=", 1)[1]
    try:
        expected = hmac.new(
            app_secret.encode("utf-8"),
            body,
            hashlib.sha256,
        ).hexdigest()
    except Exception:
        return False
    return hmac.compare_digest(expected, provided)


# ---------------------------------------------------------------------------
# Markdown stripping (URL-preserving) + chunking
# ---------------------------------------------------------------------------

_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_MD_ITAL_RE = re.compile(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)")
_MD_CODE_INLINE_RE = re.compile(r"`([^`]+)`")
_MD_CODE_BLOCK_RE = re.compile(r"```[a-zA-Z0-9_+-]*\n?(.*?)```", re.DOTALL)
_MD_HEADING_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_MD_BULLET_RE = re.compile(r"^[\s]*[-*+]\s+", re.MULTILINE)


def strip_markdown_preserving_urls(text: str) -> str:
    """Strip Markdown that Messenger can't render, keeping URLs tappable.

    Messenger text bubbles have no Markdown support — bold, italics, code
    fences, headings, and bullet markers render literally. Bare URLs are
    auto-linked by the client, but ``[label](url)`` syntax is not, so we
    convert it to ``label (url)`` and strip the rest.
    """
    if not text:
        return text
    text = _MD_CODE_BLOCK_RE.sub(lambda m: m.group(1).rstrip("\n"), text)
    text = _MD_CODE_INLINE_RE.sub(r"\1", text)
    text = _MD_LINK_RE.sub(lambda m: f"{m.group(1)} ({m.group(2)})", text)
    text = _MD_BOLD_RE.sub(r"\1", text)
    text = _MD_ITAL_RE.sub(r"\1", text)
    text = _MD_HEADING_RE.sub("", text)
    text = _MD_BULLET_RE.sub("• ", text)
    return text


def split_for_messenger(text: str, max_chars: int = MESSENGER_MAX_CHARS) -> List[str]:
    """Split ``text`` into <=``max_chars`` chunks on paragraph/line/word breaks.

    Unlike LINE, Messenger has no per-call message cap, so we keep every
    chunk (no truncation) and send them sequentially. Never splits mid-word
    when a word boundary exists within budget.
    """
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    chunks: List[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= max_chars:
            chunks.append(remaining)
            break
        cut = remaining.rfind("\n\n", 0, max_chars)
        if cut < int(max_chars * 0.5):
            cut = remaining.rfind("\n", 0, max_chars)
        if cut < int(max_chars * 0.5):
            cut = remaining.rfind(" ", 0, max_chars)
        if cut <= 0:
            cut = max_chars
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    return [c for c in chunks if c]


# ---------------------------------------------------------------------------
# Inbound dedup
# ---------------------------------------------------------------------------

class _MessageDeduplicator:
    """Bounded LRU of message ids (mids) to ignore at-least-once retries."""

    def __init__(self, max_size: int = 1000) -> None:
        self._seen: Dict[str, float] = {}
        self._max = max_size

    def is_duplicate(self, mid: str) -> bool:
        if not mid:
            return False
        if mid in self._seen:
            return True
        if len(self._seen) >= self._max:
            cutoff = sorted(self._seen.values())[len(self._seen) // 10 or 1]
            self._seen = {k: v for k, v in self._seen.items() if v > cutoff}
        self._seen[mid] = time.time()
        return False


# ---------------------------------------------------------------------------
# Graph API Send client
# ---------------------------------------------------------------------------

class _MessengerClient:
    """Thin async wrapper around the Graph API Send endpoint.

    Uses ``aiohttp`` directly to avoid an SDK dependency — only a handful of
    endpoints are needed.
    """

    def __init__(
        self,
        page_access_token: str,
        *,
        api_version: str = DEFAULT_API_VERSION,
        timeout: float = SEND_TIMEOUT,
    ) -> None:
        self._token = page_access_token
        self._version = api_version
        self._timeout = timeout

    @property
    def _send_url(self) -> str:
        return f"{GRAPH_API_BASE}/{self._version}/me/messages"

    async def send_message(
        self,
        psid: str,
        message_obj: Dict[str, Any],
        *,
        messaging_type: str = "RESPONSE",
        tag: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST a message object. Returns the Graph API JSON response.

        Raises ``RuntimeError`` on a Graph API error envelope or HTTP >= 400.
        """
        import aiohttp

        payload: Dict[str, Any] = {
            "recipient": {"id": psid},
            "messaging_type": messaging_type,
            "message": message_obj,
        }
        if tag:
            payload["tag"] = tag

        timeout = aiohttp.ClientTimeout(total=self._timeout)
        params = {"access_token": self._token}
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.post(self._send_url, params=params, json=payload) as resp:
                data = await _safe_json(resp)
                if resp.status >= 400 or (isinstance(data, dict) and "error" in data):
                    err = ""
                    if isinstance(data, dict):
                        err = json.dumps(data.get("error", data))[:300]
                    raise RuntimeError(f"Messenger send {resp.status}: {err}")
                return data if isinstance(data, dict) else {}

    async def send_action(self, psid: str, action: str) -> None:
        """Send a sender_action (typing_on / typing_off / mark_seen). Best-effort."""
        import aiohttp

        payload = {"recipient": {"id": psid}, "sender_action": action}
        timeout = aiohttp.ClientTimeout(total=5.0)
        params = {"access_token": self._token}
        try:
            async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
                await session.post(self._send_url, params=params, json=payload)
        except Exception as exc:
            logger.debug("Messenger sender_action %s failed: %s", action, exc)

    async def send_attachment_url(
        self,
        psid: str,
        attachment_type: str,
        url: str,
        *,
        messaging_type: str = "RESPONSE",
    ) -> Dict[str, Any]:
        """Send media by public URL (image/audio/video/file)."""
        message_obj = {
            "attachment": {
                "type": attachment_type,
                "payload": {"url": url, "is_reusable": True},
            }
        }
        return await self.send_message(
            psid, message_obj, messaging_type=messaging_type
        )

    async def send_attachment_file(
        self,
        psid: str,
        attachment_type: str,
        path: str,
        *,
        messaging_type: str = "RESPONSE",
    ) -> Dict[str, Any]:
        """Upload a local file as an attachment via multipart (no public URL needed)."""
        import aiohttp

        message = {
            "attachment": {"type": attachment_type, "payload": {"is_reusable": True}}
        }
        recipient = {"id": psid}

        form = aiohttp.FormData()
        form.add_field("recipient", json.dumps(recipient))
        form.add_field("message", json.dumps(message))
        form.add_field("messaging_type", messaging_type)
        with open(path, "rb") as fh:
            form.add_field(
                "filedata",
                fh.read(),
                filename=os.path.basename(path),
                content_type="application/octet-stream",
            )
        timeout = aiohttp.ClientTimeout(total=self._timeout)
        params = {"access_token": self._token}
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.post(self._send_url, params=params, data=form) as resp:
                data = await _safe_json(resp)
                if resp.status >= 400 or (isinstance(data, dict) and "error" in data):
                    err = ""
                    if isinstance(data, dict):
                        err = json.dumps(data.get("error", data))[:300]
                    raise RuntimeError(f"Messenger upload {resp.status}: {err}")
                return data if isinstance(data, dict) else {}

    async def get_page_id(self) -> Optional[str]:
        """Fetch this Page's own id for self-message filtering."""
        import aiohttp

        url = f"{GRAPH_API_BASE}/{self._version}/me"
        timeout = aiohttp.ClientTimeout(total=10.0)
        params = {"access_token": self._token, "fields": "id"}
        try:
            async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
                async with session.get(url, params=params) as resp:
                    if resp.status >= 400:
                        return None
                    data = await resp.json()
                    return data.get("id")
        except Exception:
            return None

    async def get_user_profile(self, psid: str) -> Dict[str, Any]:
        """Best-effort user profile lookup (name). Requires the profile permission."""
        import aiohttp

        url = f"{GRAPH_API_BASE}/{self._version}/{psid}"
        timeout = aiohttp.ClientTimeout(total=10.0)
        params = {"access_token": self._token, "fields": "name,first_name,last_name"}
        try:
            async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
                async with session.get(url, params=params) as resp:
                    if resp.status >= 400:
                        return {}
                    return await resp.json()
        except Exception:
            return {}

    async def fetch_attachment(self, url: str) -> bytes:
        """Download an inbound attachment's binary content from its CDN URL."""
        import aiohttp

        timeout = aiohttp.ClientTimeout(total=MEDIA_FETCH_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.get(url) as resp:
                if resp.status >= 400:
                    raise RuntimeError(f"Messenger attachment fetch {resp.status}")
                return await resp.read()


async def _safe_json(resp) -> Any:
    try:
        return await resp.json()
    except Exception:
        try:
            return {"_text": (await resp.text())[:300]}
        except Exception:
            return {}


# ---------------------------------------------------------------------------
# Message builders
# ---------------------------------------------------------------------------

def _text_message(text: str) -> Dict[str, Any]:
    """Build a Messenger text message object, capped to the per-message max."""
    if len(text) > MESSENGER_MAX_CHARS:
        text = text[: MESSENGER_MAX_CHARS - 1] + "…"
    return {"text": text}


# Map Messenger attachment types to (cache fn, MessageType) for inbound media.
def _cache_inbound(attachment_type: str, data: bytes, fallback_name: str) -> Tuple[str, str]:
    """Cache inbound media bytes; return (local_path, media_type_label)."""
    if attachment_type == "image":
        return cache_image_from_bytes(data, ext=".jpg"), "image"
    if attachment_type == "audio":
        return cache_audio_from_bytes(data, ext=".mp4"), "audio"
    if attachment_type == "video":
        return cache_video_from_bytes(data, ext=".mp4"), "video"
    # file / fallback
    return cache_document_from_bytes(data, fallback_name), "file"


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class MessengerAdapter(BasePlatformAdapter):
    def __init__(self, config, **kwargs):
        super().__init__(config=config, platform=Platform("messenger"))

        extra = getattr(config, "extra", {}) or {}

        # Credentials
        self.page_access_token = (
            os.getenv("MESSENGER_PAGE_ACCESS_TOKEN")
            or extra.get("page_access_token", "")
        )
        self.app_secret = (
            os.getenv("MESSENGER_APP_SECRET") or extra.get("app_secret", "")
        )
        self.verify_token = (
            os.getenv("MESSENGER_VERIFY_TOKEN") or extra.get("verify_token", "")
        )
        self.api_version = (
            os.getenv("MESSENGER_API_VERSION")
            or extra.get("api_version", DEFAULT_API_VERSION)
        )

        # Webhook server
        self.webhook_host = os.getenv("MESSENGER_HOST") or extra.get("host", "0.0.0.0")
        try:
            self.webhook_port = int(
                os.getenv("MESSENGER_PORT") or extra.get("port", DEFAULT_WEBHOOK_PORT)
            )
        except (TypeError, ValueError):
            self.webhook_port = DEFAULT_WEBHOOK_PORT
        self.webhook_path = extra.get("webhook_path", DEFAULT_WEBHOOK_PATH)
        self.public_base_url = (
            os.getenv("MESSENGER_PUBLIC_URL") or extra.get("public_url", "") or ""
        ).rstrip("/")

        # Allowlist
        self.allow_all = _truthy_env(
            "MESSENGER_ALLOW_ALL_USERS", bool(extra.get("allow_all_users", False))
        )
        self.allowed_users = _csv_set(
            os.getenv("MESSENGER_ALLOWED_USERS", "")
        ) | set(extra.get("allowed_users", []))

        # Runtime state
        self._client: Optional[_MessengerClient] = None
        self._app = None
        self._runner = None
        self._site = None
        self._dedup = _MessageDeduplicator()
        self._page_id: Optional[str] = None
        self._lock_key: Optional[str] = None

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        if not (self.page_access_token and self.app_secret and self.verify_token):
            self._set_fatal_error(
                "config_missing",
                "MESSENGER_PAGE_ACCESS_TOKEN, MESSENGER_APP_SECRET and "
                "MESSENGER_VERIFY_TOKEN must all be set",
                retryable=False,
            )
            return False

        # Prevent two profiles from binding the same Page token.
        try:
            from gateway.status import acquire_scoped_lock

            tok_hash = hashlib.sha256(self.page_access_token.encode()).hexdigest()[:16]
            acquired, _meta = acquire_scoped_lock("messenger", tok_hash)
            if not acquired:
                self._set_fatal_error(
                    "lock_conflict",
                    "Messenger Page already in use by another profile",
                    retryable=False,
                )
                return False
            self._lock_key = tok_hash
        except ImportError:
            self._lock_key = None

        self._client = _MessengerClient(
            self.page_access_token, api_version=self.api_version
        )

        # Best-effort: fetch our own Page id for self-message filtering.
        try:
            self._page_id = await self._client.get_page_id()
        except Exception as exc:
            logger.debug("Messenger: get_page_id failed: %s", exc)
            self._page_id = None

        try:
            from aiohttp import web
        except ImportError:
            self._set_fatal_error(
                "missing_dep",
                "aiohttp is required for the Messenger adapter — `pip install aiohttp`",
                retryable=False,
            )
            return False

        self._app = web.Application(client_max_size=WEBHOOK_BODY_MAX_BYTES)
        self._app.router.add_get(self.webhook_path, self._handle_verify)
        self._app.router.add_post(self.webhook_path, self._handle_webhook)
        self._app.router.add_get(f"{self.webhook_path}/health", self._handle_health)

        self._runner = web.AppRunner(self._app)
        try:
            await self._runner.setup()
            self._site = web.TCPSite(self._runner, self.webhook_host, self.webhook_port)
            await self._site.start()
        except OSError as exc:
            self._set_fatal_error(
                "bind_failed",
                f"Could not bind Messenger webhook on "
                f"{self.webhook_host}:{self.webhook_port}: {exc}",
                retryable=True,
            )
            return False

        self._mark_connected()
        logger.info(
            "Messenger: webhook listening on %s:%s%s%s",
            self.webhook_host,
            self.webhook_port,
            self.webhook_path,
            f" (public: {self.public_base_url})" if self.public_base_url else "",
        )
        return True

    async def disconnect(self) -> None:
        self._mark_disconnected()

        if self._site is not None:
            try:
                await self._site.stop()
            except Exception:
                pass
            self._site = None
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception:
                pass
            self._runner = None
        self._app = None

        if self._lock_key:
            try:
                from gateway.status import release_scoped_lock

                release_scoped_lock("messenger", self._lock_key)
            except Exception:
                pass
            self._lock_key = None

    # ------------------------------------------------------------------
    # Webhook handlers
    # ------------------------------------------------------------------

    async def _handle_health(self, request) -> Any:
        from aiohttp import web

        return web.json_response({"status": "ok", "platform": "messenger"})

    async def _handle_verify(self, request) -> Any:
        """Meta GET verification handshake. Echo hub.challenge when token matches."""
        from aiohttp import web

        params = request.rel_url.query
        if (
            params.get("hub.mode") == "subscribe"
            and params.get("hub.verify_token") == self.verify_token
        ):
            return web.Response(status=200, text=params.get("hub.challenge", ""))
        return web.Response(status=403, text="verification failed")

    async def _handle_webhook(self, request) -> Any:
        from aiohttp import web

        try:
            body = await request.read()
        except Exception as exc:
            logger.debug("Messenger: read failed: %s", exc)
            return web.Response(status=400, text="bad request")
        if len(body) > WEBHOOK_BODY_MAX_BYTES:
            return web.Response(status=413, text="payload too large")

        signature = request.headers.get("X-Hub-Signature-256", "")
        if not verify_messenger_signature(body, signature, self.app_secret):
            return web.Response(status=401, text="invalid signature")

        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return web.Response(status=400, text="bad json")

        # Only handle Page subscriptions.
        if payload.get("object") != "page":
            return web.Response(status=200, text="ignored")

        for entry in payload.get("entry", []) or []:
            for messaging in entry.get("messaging", []) or []:
                try:
                    await self._dispatch_event(messaging)
                except Exception:
                    logger.exception("Messenger: dispatch_event failed")

        # Always 200 — Meta retries non-200 aggressively and disables webhooks
        # after repeated failures.
        return web.Response(status=200, text="EVENT_RECEIVED")

    async def _dispatch_event(self, messaging: Dict[str, Any]) -> None:
        sender = (messaging.get("sender") or {}).get("id", "")
        message = messaging.get("message") or {}
        postback = messaging.get("postback")

        # Drop echoes (our own outbound) and Page-sender events — reply loops.
        if message.get("is_echo"):
            return
        if self._page_id and sender == self._page_id:
            return

        # Dedup on mid (messages only; postbacks carry no mid).
        mid = message.get("mid", "")
        if mid and self._dedup.is_duplicate(mid):
            logger.debug("Messenger: ignoring duplicate mid %s", mid)
            return

        # Allowlist gate.
        if not self.allow_all and sender not in self.allowed_users:
            logger.info("Messenger: rejecting unauthorized PSID %s", sender)
            return

        if message:
            await self._handle_message_event(messaging)
        elif postback:
            await self._handle_postback_event(messaging)
        else:
            logger.debug("Messenger: ignoring non-message event keys=%s", list(messaging.keys()))

    async def _handle_message_event(self, messaging: Dict[str, Any]) -> None:
        sender = (messaging.get("sender") or {}).get("id", "")
        message = messaging.get("message") or {}
        mid = message.get("mid", "")
        text = message.get("text", "") or ""

        media_urls: List[str] = []
        media_types: List[str] = []

        for att in message.get("attachments", []) or []:
            att_type = att.get("type", "")
            payload = att.get("payload") or {}
            url = payload.get("url", "")
            if att_type in {"image", "audio", "video", "file"} and url and self._client:
                try:
                    data = await self._client.fetch_attachment(url)
                    local_path, label = _cache_inbound(
                        att_type, data, f"messenger_{mid or 'file'}"
                    )
                    media_urls.append(local_path)
                    media_types.append(label)
                    if not text:
                        text = f"[{att_type}]"
                except Exception as exc:
                    logger.warning("Messenger: attachment fetch failed: %s", exc)
                    if not text:
                        text = f"[{att_type} attachment]"
            elif att_type == "location":
                coords = (payload.get("coordinates") or {})
                text = (
                    f"[location: {coords.get('lat', '')},{coords.get('long', '')}]"
                    if coords
                    else "[location]"
                )
            elif not text:
                text = f"[{att_type or 'unsupported'} attachment]"

        # Best-effort: mark seen + typing.
        if self._client and sender:
            asyncio.create_task(self._client.send_action(sender, "mark_seen"))
            asyncio.create_task(self._client.send_action(sender, "typing_on"))

        # Best-effort profile name.
        user_name = sender
        if self._client and sender:
            try:
                profile = await self._client.get_user_profile(sender)
                user_name = profile.get("name") or sender
            except Exception:
                user_name = sender

        source_obj = self.build_source(
            chat_id=sender,
            chat_type="dm",
            user_id=sender,
            user_name=user_name,
            chat_name=user_name,
        )

        has_media = bool(media_urls)
        event_obj = MessageEvent(
            text=text,
            message_type=MessageType.PHOTO if has_media else MessageType.TEXT,
            source=source_obj,
            raw_message=messaging,
            message_id=mid,
            media_urls=media_urls,
            media_types=media_types,
        )
        await self.handle_message(event_obj)

    async def _handle_postback_event(self, messaging: Dict[str, Any]) -> None:
        sender = (messaging.get("sender") or {}).get("id", "")
        postback = messaging.get("postback") or {}
        text = postback.get("payload") or postback.get("title") or ""
        if not text:
            return

        source_obj = self.build_source(
            chat_id=sender, chat_type="dm", user_id=sender,
            user_name=sender, chat_name=sender,
        )
        event_obj = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source_obj,
            raw_message=messaging,
            message_id="",
        )
        await self.handle_message(event_obj)

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        if not self._client:
            return SendResult(success=False, error="Messenger adapter not connected")

        chunks = split_for_messenger(strip_markdown_preserving_urls(content))
        if not chunks:
            return SendResult(success=True, message_id=None)

        last_id = None
        for chunk in chunks:
            try:
                resp = await self._client.send_message(chat_id, _text_message(chunk))
                last_id = resp.get("message_id") or last_id
            except Exception as exc:
                logger.error("Messenger send failed: %s", exc)
                return SendResult(success=False, error=str(exc))
        return SendResult(success=True, message_id=last_id)

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        if self._client and chat_id:
            await self._client.send_action(chat_id, "typing_on")

    async def send_image(
        self, chat_id: str, image_url: str, caption: str = "", metadata=None
    ) -> SendResult:
        if not self._client:
            return SendResult(success=False, error="Messenger adapter not connected")
        try:
            if caption:
                await self._client.send_message(chat_id, _text_message(caption))
            resp = await self._client.send_attachment_url(chat_id, "image", image_url)
            return SendResult(success=True, message_id=resp.get("message_id"))
        except Exception as exc:
            logger.error("Messenger send_image failed: %s", exc)
            return SendResult(success=False, error=str(exc))

    async def send_image_file(
        self, chat_id: str, path: str, caption: str = "", metadata=None
    ) -> SendResult:
        return await self._send_file(chat_id, "image", path, caption)

    async def send_document(
        self, chat_id: str, path: str, caption: str = "", metadata=None
    ) -> SendResult:
        return await self._send_file(chat_id, "file", path, caption)

    async def send_voice(self, chat_id: str, path: str, metadata=None) -> SendResult:
        return await self._send_file(chat_id, "audio", path, "")

    async def send_video(
        self, chat_id: str, path: str, caption: str = "", metadata=None
    ) -> SendResult:
        return await self._send_file(chat_id, "video", path, caption)

    async def _send_file(
        self, chat_id: str, att_type: str, path: str, caption: str
    ) -> SendResult:
        if not self._client:
            return SendResult(success=False, error="Messenger adapter not connected")
        try:
            if caption:
                await self._client.send_message(chat_id, _text_message(caption))
            resp = await self._client.send_attachment_file(chat_id, att_type, path)
            return SendResult(success=True, message_id=resp.get("message_id"))
        except Exception as exc:
            logger.error("Messenger send %s file failed: %s", att_type, exc)
            return SendResult(success=False, error=str(exc))

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        name = chat_id
        if self._client:
            try:
                profile = await self._client.get_user_profile(chat_id)
                name = profile.get("name") or chat_id
            except Exception:
                name = chat_id
        return {"name": name, "type": "dm", "chat_id": chat_id}


# ---------------------------------------------------------------------------
# Plugin lifecycle / registry functions
# ---------------------------------------------------------------------------

def check_requirements() -> bool:
    """Plugin gate: require credentials AND aiohttp at runtime."""
    if not os.getenv("MESSENGER_PAGE_ACCESS_TOKEN"):
        return False
    if not os.getenv("MESSENGER_APP_SECRET"):
        return False
    if not os.getenv("MESSENGER_VERIFY_TOKEN"):
        return False
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        return False
    return True


def validate_config(config) -> bool:
    extra = getattr(config, "extra", {}) or {}
    has_token = bool(
        os.getenv("MESSENGER_PAGE_ACCESS_TOKEN") or extra.get("page_access_token")
    )
    has_secret = bool(os.getenv("MESSENGER_APP_SECRET") or extra.get("app_secret"))
    has_verify = bool(os.getenv("MESSENGER_VERIFY_TOKEN") or extra.get("verify_token"))
    return has_token and has_secret and has_verify


def is_connected(config) -> bool:
    """Surface in ``hermes status`` even before the adapter is instantiated."""
    return validate_config(config)


def _env_enablement() -> Optional[Dict[str, Any]]:
    """Auto-seed PlatformConfig.extra from an env-only setup."""
    if not (
        os.getenv("MESSENGER_PAGE_ACCESS_TOKEN")
        and os.getenv("MESSENGER_APP_SECRET")
        and os.getenv("MESSENGER_VERIFY_TOKEN")
    ):
        return None
    seeded: Dict[str, Any] = {}
    if os.getenv("MESSENGER_PORT"):
        try:
            seeded["port"] = int(os.environ["MESSENGER_PORT"])
        except ValueError:
            pass
    if os.getenv("MESSENGER_HOST"):
        seeded["host"] = os.environ["MESSENGER_HOST"]
    if os.getenv("MESSENGER_PUBLIC_URL"):
        seeded["public_url"] = os.environ["MESSENGER_PUBLIC_URL"]
    if os.getenv("MESSENGER_API_VERSION"):
        seeded["api_version"] = os.environ["MESSENGER_API_VERSION"]
    if os.getenv("MESSENGER_HOME_CHANNEL"):
        seeded["home_channel"] = os.environ["MESSENGER_HOME_CHANNEL"]
    return seeded or {}


async def _standalone_send(
    pconfig,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[List[str]] = None,
    force_document: bool = False,
) -> Dict[str, Any]:
    """Out-of-process Send API delivery for detached cron jobs.

    Without this, ``deliver=messenger`` cron jobs fail with ``no live
    adapter`` when cron runs as its own process.

    NOTE on the 24-hour window: a plain ``messaging_type=RESPONSE`` only
    succeeds within 24h of the user's last message. Proactive sends outside
    that window require an approved message tag; set ``MESSENGER_CRON_TAG``
    (e.g. ``CONFIRMED_EVENT_UPDATE``) to attach one.
    """
    extra = getattr(pconfig, "extra", {}) or {}
    token = (
        os.getenv("MESSENGER_PAGE_ACCESS_TOKEN")
        or extra.get("page_access_token", "")
    )
    if not token or not chat_id:
        return {"error": "Messenger standalone send: missing token or chat_id"}

    api_version = (
        os.getenv("MESSENGER_API_VERSION")
        or extra.get("api_version", DEFAULT_API_VERSION)
    )
    cron_tag = os.getenv("MESSENGER_CRON_TAG") or extra.get("cron_tag")
    messaging_type = "MESSAGE_TAG" if cron_tag else "RESPONSE"

    plain = strip_markdown_preserving_urls(message or "")
    chunks = split_for_messenger(plain) or [""]
    client = _MessengerClient(token, api_version=api_version)

    last_id = None
    try:
        for chunk in chunks:
            resp = await client.send_message(
                chat_id,
                _text_message(chunk),
                messaging_type=messaging_type,
                tag=cron_tag,
            )
            last_id = resp.get("message_id") or last_id
        if media_files:
            for path in media_files:
                try:
                    await client.send_attachment_file(
                        chat_id, "file", path, messaging_type=messaging_type
                    )
                except Exception as exc:
                    logger.warning("Messenger cron media send failed: %s", exc)
        return {"success": True, "message_id": last_id}
    except Exception as exc:
        return {"error": str(exc)}


def interactive_setup() -> None:
    """Minimal stdin wizard for ``hermes setup messenger``."""
    print()
    print("Facebook Messenger setup")
    print("------------------------")
    print("Create a Meta app + Messenger product at "
          "https://developers.facebook.com/apps/")
    print("then connect a Page and copy the values below.")
    print()

    try:
        from hermes_cli.config import get_env_var, set_env_var
    except ImportError:
        print("hermes_cli.config not available; set MESSENGER_* vars manually "
              "in ~/.hermes/.env")
        return

    def _prompt(var: str, prompt: str, *, secret: bool = False) -> None:
        existing = get_env_var(var) if callable(get_env_var) else None
        suffix = " [keep current]" if existing else ""
        try:
            if secret:
                from hermes_cli.secret_prompt import masked_secret_prompt

                value = masked_secret_prompt(f"{prompt}{suffix}: ")
            else:
                value = input(f"{prompt}{suffix}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if value:
            set_env_var(var, value)

    _prompt("MESSENGER_PAGE_ACCESS_TOKEN", "Page access token", secret=True)
    _prompt("MESSENGER_APP_SECRET", "App secret", secret=True)
    _prompt("MESSENGER_VERIFY_TOKEN", "Webhook verify token (any random string)", secret=True)
    _prompt("MESSENGER_PUBLIC_URL", "Public HTTPS base URL (e.g. https://my-tunnel.example.com)")
    _prompt("MESSENGER_ALLOWED_USERS", "Allowed PSIDs (comma-separated; blank=skip)")
    print()
    print("Done. In the Meta console set the webhook Callback URL to "
          "<your-public-url>/messenger/webhook, paste the same Verify Token, "
          "subscribe the fields 'messages' and 'messaging_postbacks', and "
          "subscribe your Page.")


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system at startup."""
    ctx.register_platform(
        name="messenger",
        label="Messenger",
        adapter_factory=lambda cfg: MessengerAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=[
            "MESSENGER_PAGE_ACCESS_TOKEN",
            "MESSENGER_APP_SECRET",
            "MESSENGER_VERIFY_TOKEN",
        ],
        install_hint="pip install aiohttp",
        setup_fn=interactive_setup,
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="MESSENGER_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="MESSENGER_ALLOWED_USERS",
        allow_all_env="MESSENGER_ALLOW_ALL_USERS",
        max_message_length=MESSENGER_MAX_CHARS,
        emoji="💬",
        pii_safe=False,
        allow_update_command=True,
        platform_hint=(
            "You are chatting via Facebook Messenger (1:1 Page DM). Messenger "
            "does NOT render Markdown — ** and # show literally; bare URLs "
            "auto-link but [label](url) syntax does not. Each message is capped "
            "at 2000 characters; longer replies are split into multiple bubbles. "
            "You can only reply within 24 hours of the user's last message."
        ),
    )

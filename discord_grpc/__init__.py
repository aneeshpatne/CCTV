"""Discord webhook gRPC client.

Thin wrapper around the generated `discord_webhook_pb2` / `discord_webhook_pb2_grpc`
stubs. Sends messages, images, and videos to a Discord channel through an external
gRPC server (default `127.0.0.1:50051`) which translates the RPC into a Discord
webhook HTTP call.

Configuration:
    DISCORD_GRPC_TARGET   Address of the gRPC server. Default "127.0.0.1:50051".
    DISCORD_CHANNEL       Channel name sent in every request. Default "cctv".
    DISCORD_USERNAME      Optional override for the displayed webhook username.
    DISCORD_AVATAR_URL    Optional override for the displayed webhook avatar.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Iterable, Optional

import grpc

from . import discord_webhook_pb2 as pb
from . import discord_webhook_pb2_grpc as pb_grpc

__all__ = [
    "send_text",
    "send_image",
    "send_video",
    "send_file",
    "get_stub",
]

logger = logging.getLogger(__name__)

DEFAULT_TARGET = os.getenv("DISCORD_GRPC_TARGET", "127.0.0.1:50051")
DEFAULT_CHANNEL = os.getenv("DISCORD_CHANNEL", "cctv")
DEFAULT_USERNAME = os.getenv("DISCORD_USERNAME") or None
DEFAULT_AVATAR_URL = os.getenv("DISCORD_AVATAR_URL") or None

# Discord's standard webhook upload limit is ~10 MB. We target 9.5 MB to leave
# headroom for the multipart envelope; boost the server to lift this cap.
DISCORD_FILE_LIMIT_BYTES = 9 * 1024 * 1024 + 512 * 1024  # 9.5 MB

# gRPC channel shared across calls. Created lazily.
_channel: Optional[grpc.Channel] = None
_channel_lock: Optional["object"] = None
if _channel_lock is None:
    import threading

    _channel_lock = threading.Lock()


def _get_channel() -> grpc.Channel:
    global _channel
    if _channel is not None:
        return _channel
    with _channel_lock:  # type: ignore[union-attr]
        if _channel is None:
            logger.info("[discord_grpc] connecting to %s", DEFAULT_TARGET)
            _channel = grpc.insecure_channel(
                DEFAULT_TARGET,
                options=[
                    ("grpc.max_send_message_length", 64 * 1024 * 1024),
                    ("grpc.max_receive_message_length", 16 * 1024 * 1024),
                    ("grpc.keepalive_time_ms", 30_000),
                    ("grpc.keepalive_timeout_ms", 10_000),
                ],
            )
    return _channel


def get_stub() -> pb_grpc.DiscordWebhookStub:
    return pb_grpc.DiscordWebhookStub(_get_channel())


def _build_embeds(embeds: Optional[Iterable[dict]]) -> list[pb.Embed]:
    if not embeds:
        return []

    built: list[pb.Embed] = []
    for e in embeds:
        if not isinstance(e, dict):
            continue
        kwargs: dict = {}
        for key in ("title", "description", "url", "color", "timestamp"):
            if key in e and e[key] is not None:
                kwargs[key] = e[key]
        if e.get("footer"):
            kwargs["footer"] = pb.EmbedFooter(**e["footer"])
        if e.get("image"):
            kwargs["image"] = pb.EmbedImage(**e["image"])
        if e.get("thumbnail"):
            kwargs["thumbnail"] = pb.EmbedThumbnail(**e["thumbnail"])
        if e.get("author"):
            kwargs["author"] = pb.EmbedAuthor(**e["author"])
        if e.get("fields"):
            kwargs["fields"] = [pb.EmbedField(**f) for f in e["fields"]]
        built.append(pb.Embed(**kwargs))
    return built


def _allowed_mentions(value: Optional[dict]) -> Optional[pb.AllowedMentions]:
    if not value:
        return None
    return pb.AllowedMentions(
        parse=list(value.get("parse", []) or []),
        users=list(value.get("users", []) or []),
        roles=list(value.get("roles", []) or []),
        replied_user=value.get("replied_user"),
    )


def _rpc_call(call, request, *, timeout: float = 120.0, retries: int = 3, backoff: float = 1.5):
    last_exc: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            return call(request, timeout=timeout)
        except grpc.RpcError as exc:
            last_exc = exc
            code = exc.code() if hasattr(exc, "code") else None
            # Retry only on transient transport-level errors.
            retryable = code in (
                grpc.StatusCode.UNAVAILABLE,
                grpc.StatusCode.DEADLINE_EXCEEDED,
                grpc.StatusCode.RESOURCE_EXHAUSTED,
                grpc.StatusCode.ABORTED,
            )
            if not retryable or attempt == retries:
                logger.error("[discord_grpc] RPC failed: %s", exc)
                raise
            time.sleep(backoff * attempt)
    if last_exc:
        raise last_exc
    raise RuntimeError("[discord_grpc] unreachable")


def send_text(
    content: str,
    *,
    channel: Optional[str] = None,
    username: Optional[str] = None,
    avatar_url: Optional[str] = None,
    embeds: Optional[Iterable[dict]] = None,
    allowed_mentions: Optional[dict] = None,
    timeout: float = 60.0,
) -> pb.SendResponse:
    request = pb.SendTextRequest(
        channel_name=channel or DEFAULT_CHANNEL,
        content=content,
        username=username or DEFAULT_USERNAME,
        avatar_url=avatar_url or DEFAULT_AVATAR_URL,
        embeds=_build_embeds(embeds),
        allowed_mentions=_allowed_mentions(allowed_mentions),
    )
    stub = get_stub()
    return _rpc_call(stub.SendText, request, timeout=timeout)


def send_image(
    path: str | Path,
    *,
    content: Optional[str] = None,
    channel: Optional[str] = None,
    username: Optional[str] = None,
    avatar_url: Optional[str] = None,
    embeds: Optional[Iterable[dict]] = None,
    allowed_mentions: Optional[dict] = None,
    timeout: float = 120.0,
) -> pb.SendResponse:
    path = Path(path)
    with open(path, "rb") as fh:
        data = fh.read()
    request = pb.SendImageRequest(
        channel_name=channel or DEFAULT_CHANNEL,
        data=data,
        filename=path.name,
        content=content,
        username=username or DEFAULT_USERNAME,
        avatar_url=avatar_url or DEFAULT_AVATAR_URL,
        embeds=_build_embeds(embeds),
        allowed_mentions=_allowed_mentions(allowed_mentions),
    )
    stub = get_stub()
    return _rpc_call(stub.SendImage, request, timeout=timeout)


def send_video(
    path: str | Path,
    *,
    content: Optional[str] = None,
    channel: Optional[str] = None,
    username: Optional[str] = None,
    avatar_url: Optional[str] = None,
    embeds: Optional[Iterable[dict]] = None,
    allowed_mentions: Optional[dict] = None,
    timeout: float = 300.0,
) -> pb.SendResponse:
    path = Path(path)
    with open(path, "rb") as fh:
        data = fh.read()
    if len(data) > DISCORD_FILE_LIMIT_BYTES:
        raise ValueError(
            f"Video {path.name} is {len(data) / 1024 / 1024:.1f} MB, exceeds "
            f"Discord's 25 MB limit. Compress first."
        )
    request = pb.SendVideoRequest(
        channel_name=channel or DEFAULT_CHANNEL,
        data=data,
        filename=path.name,
        content=content,
        username=username or DEFAULT_USERNAME,
        avatar_url=avatar_url or DEFAULT_AVATAR_URL,
        embeds=_build_embeds(embeds),
        allowed_mentions=_allowed_mentions(allowed_mentions),
    )
    stub = get_stub()
    return _rpc_call(stub.SendVideo, request, timeout=timeout)


def send_file(path: str | Path, *, content: Optional[str] = None, **kwargs) -> pb.SendResponse:
    """Dispatch to send_image or send_video based on file extension."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in (".mp4", ".mov", ".mkv", ".webm", ".avi"):
        return send_video(path, content=content, **kwargs)
    if suffix in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        return send_image(path, content=content, **kwargs)
    raise ValueError(f"Unsupported file type: {suffix}")
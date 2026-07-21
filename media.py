"""File upload/download: the two-step rooms.media flow and media sends."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import unicodedata
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import quote, urlsplit

from gateway.platforms.base import SendResult

from .helpers import (
    MediaDownloadTooLarge,
    is_valid_server_identifier,
    is_valid_url_path_identifier,
    read_bounded_json_response,
    read_bounded_response_bytes,
    validate_auth_config,
)

logger = logging.getLogger(__name__)


def _is_public_media_address(raw_address: Any) -> bool:
    """Reject every non-global address returned by the connection-time resolver."""
    if not isinstance(raw_address, str):
        return False
    try:
        address = ipaddress.ip_address(raw_address.split("%", 1)[0])
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    return bool(address.is_global)


class _PublicOnlyResolver:
    """Validate the DNS answer actually used by aiohttp, closing rebinding TOCTOU."""

    def __init__(self, delegate: Any):
        self._delegate = delegate

    async def resolve(self, host: str, port: int = 0, family: int = 0):
        records = await self._delegate.resolve(host, port, family)
        if not isinstance(records, list) or not records:
            raise OSError("media DNS resolution returned no addresses")
        if any(
            not isinstance(record, dict)
            or not _is_public_media_address(record.get("host"))
            for record in records
        ):
            raise OSError("media DNS resolution returned a non-public address")
        return records

    async def close(self) -> None:
        close = getattr(self._delegate, "close", None)
        if callable(close):
            result = close()
            if asyncio.iscoroutine(result):
                await result


def _safe_external_media_url(url: Any) -> bool:
    if not isinstance(url, str) or not url or len(url) > 8192:
        return False
    if any(
        unicodedata.category(char) in {"Cc", "Cf", "Cs"}
        for char in url
    ):
        return False
    try:
        parsed = urlsplit(url)
        parsed.port
    except (TypeError, ValueError):
        return False
    return bool(
        parsed.scheme.lower() in {"http", "https"}
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
        and "@" not in parsed.netloc
    )


class MediaMixin:
    """Media sending for :class:`~.adapter.RocketchatAdapter`."""

    async def _upload_file(
        self,
        room_id: str,
        file_data: bytes,
        filename: str,
        content_type: str,
        caption: Optional[str] = None,
        tmid: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """Upload a file via the two-step rooms.media flow.

        Step 1 uploads the bytes; step 2 confirms and creates the message.
        Returns the message _id on success, None on failure.
        """
        import aiohttp

        try:
            self._base_url, self._token, self._bot_user_id = (
                validate_auth_config(
                    self._base_url, self._token, self._bot_user_id
                )
            )
        except ValueError:
            logger.error("Rocket.Chat media upload configuration is invalid")
            return None
        if not is_valid_url_path_identifier(room_id):
            logger.error("Rocket.Chat media upload room id is invalid")
            return None

        # Step 1: upload the file bytes.
        step1_url = (
            f"{self._base_url}/api/v1/rooms.media/"
            f"{quote(str(room_id), safe='')}"
        )
        form = aiohttp.FormData()
        form.add_field(
            "file",
            file_data,
            filename=filename,
            content_type=content_type,
        )
        headers = {
            "X-Auth-Token": self._token,
            "X-User-Id": self._bot_user_id,
        }
        try:
            async with self._session.post(
                step1_url, headers=headers, data=form,
                timeout=aiohttp.ClientTimeout(total=120),
                allow_redirects=False,
            ) as resp:
                if resp.status < 200 or resp.status >= 300:
                    logger.error(
                        "RC rooms.media rejected with HTTP %s", resp.status
                    )
                    return None
                step1 = await read_bounded_json_response(resp)
        except (
            aiohttp.ClientError,
            asyncio.TimeoutError,
            TypeError,
            ValueError,
        ) as exc:
            logger.error("RC rooms.media failed (%s)", type(exc).__name__)
            return None

        if not isinstance(step1, dict) or step1.get("success", True) is not True:
            logger.error("RC rooms.media returned an invalid response")
            return None
        uploaded_file = step1.get("file")
        file_id = uploaded_file.get("_id") if isinstance(uploaded_file, dict) else None
        if not is_valid_url_path_identifier(file_id):
            logger.error("RC rooms.media returned no file id")
            return None

        # Step 2: confirm — this creates the message.
        step2_path = (
            f"rooms.mediaConfirm/{quote(str(room_id), safe='')}/"
            f"{quote(str(file_id), safe='')}"
        )
        payload: Dict[str, Any] = {}
        if caption:
            payload["msg"] = caption
        thread_target = await self._thread_target_for_reply(
            room_id, tmid, metadata
        )
        if thread_target:
            payload["tmid"] = thread_target
        step2 = await self._api_post(step2_path, payload)
        if not isinstance(step2, dict) or step2.get("success", True) is not True:
            return None
        msg = step2.get("message")
        if (
            not isinstance(msg, dict)
            or not is_valid_server_identifier(msg.get("_id"))
            or msg.get("rid") != room_id
            or (thread_target and msg.get("tmid") != thread_target)
        ):
            logger.error("RC rooms.mediaConfirm returned an invalid message target")
            return None
        return msg["_id"]

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Download an image and upload it as a file attachment."""
        return await self._send_url_as_file(
            chat_id, image_url, caption, reply_to, "image", metadata
        )

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Upload a local image file."""
        return await self._send_local_file(
            chat_id, image_path, caption, reply_to, metadata=metadata
        )

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Upload a local file as a document."""
        return await self._send_local_file(
            chat_id, file_path, caption, reply_to, file_name, metadata
        )

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Upload an audio file."""
        return await self._send_local_file(
            chat_id, audio_path, caption, reply_to, metadata=metadata
        )

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Upload a video file."""
        return await self._send_local_file(
            chat_id, video_path, caption, reply_to, metadata=metadata
        )

    # ------------------------------------------------------------------
    # File helpers
    # ------------------------------------------------------------------

    async def _send_url_as_file(
        self,
        chat_id: str,
        url: str,
        caption: Optional[str],
        reply_to: Optional[str],
        kind: str = "file",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Download a URL and upload it as a file attachment."""
        from tools.url_safety import is_safe_url
        if not _safe_external_media_url(url) or not is_safe_url(url):
            logger.warning("Rocket.Chat: blocked unsafe URL (SSRF protection)")
            return SendResult(
                success=False, error="Media URL was blocked by the safety policy"
            )

        import aiohttp

        file_data = None
        ct = "application/octet-stream"
        parsed = urlsplit(url)
        fname = Path(parsed.path).name or f"{kind}.bin"
        if any(
            unicodedata.category(char) in {"Cc", "Cf", "Cs"}
            for char in fname
        ):
            fname = f"{kind}.bin"

        delegate = aiohttp.resolver.DefaultResolver()
        resolver = _PublicOnlyResolver(delegate)
        connector = aiohttp.TCPConnector(
            resolver=resolver, use_dns_cache=False
        )
        try:
            async with aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=30),
                trust_env=False,
            ) as download_session:
                for attempt in range(3):
                    try:
                        async with download_session.get(
                            url,
                            allow_redirects=False,
                        ) as resp:
                            if resp.status >= 500 or resp.status == 429:
                                if attempt < 2:
                                    await asyncio.sleep(1.5 * (attempt + 1))
                                    continue
                            if resp.status < 200 or resp.status >= 300:
                                return SendResult(
                                    success=False,
                                    error="Media URL download was rejected",
                                )
                            file_data = await read_bounded_response_bytes(resp)
                            response_type = resp.content_type
                            ct = (
                                response_type
                                if isinstance(response_type, str)
                                and response_type
                                else "application/octet-stream"
                            )
                            break
                    except MediaDownloadTooLarge:
                        logger.warning(
                            "Rocket.Chat: media URL exceeded the download limit"
                        )
                        return SendResult(
                            success=False, error="Media URL exceeded the download limit"
                        )
                    except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
                        if attempt < 2:
                            await asyncio.sleep(1.5 * (attempt + 1))
                            continue
                        return SendResult(
                            success=False, error="Media URL download failed"
                        )
        finally:
            if not connector.closed:
                await connector.close()
            # aiohttp does not own custom resolver instances, so explicitly
            # release aiodns/threaded resolver resources.
            try:
                await resolver.close()
            except Exception as exc:
                logger.warning(
                    "Rocket.Chat: media resolver close failed (%s)",
                    type(exc).__name__,
                )

        if file_data is None:
            return SendResult(
                success=False, error="Media URL download returned no data"
            )

        msg_id = await self._upload_file(
            chat_id, file_data, fname, ct, caption, reply_to, metadata,
        )
        if not msg_id:
            return SendResult(success=False, error="Media upload failed")
        return SendResult(success=True, message_id=msg_id)

    async def _send_local_file(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str],
        reply_to: Optional[str],
        file_name: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Upload a local file via the two-step rooms.media flow."""
        import mimetypes

        # Model-emitted MEDIA:/path directives reach this method outside the
        # agent-tool registry.  Reapply the exact same host-file capability,
        # task-local requester scope, secure descriptor walk, and size guard.
        from .tools import (
            _authorize_write_scope,
            _authorized_file_path,
            _file_operation_semaphore,
            _guard_write_tool,
            _has_unsafe_control,
            _max_agent_file_bytes,
            _read_regular_file,
            file_uploads_enabled,
        )

        guard = _guard_write_tool("rocketchat_gateway_send_file", chat_id)
        if guard or not file_uploads_enabled():
            return SendResult(success=False, error="Local file delivery is not authorized")
        if _authorize_write_scope(
            "rocketchat_gateway_send_file", privileged=True
        ) or _authorize_write_scope(
            "rocketchat_gateway_send_file", room_id=chat_id
        ):
            return SendResult(success=False, error="Local file delivery is not authorized")
        plan, path_error = _authorized_file_path(file_path)
        if path_error or plan is None:
            return SendResult(success=False, error="Local file delivery is not authorized")

        requested_name = Path(file_name).name if file_name else plan.name
        if not requested_name or _has_unsafe_control(requested_name):
            return SendResult(success=False, error="Local file name is invalid")
        ct = mimetypes.guess_type(requested_name)[0] or "application/octet-stream"
        semaphore = _file_operation_semaphore()
        await semaphore.acquire()
        release_permit = True
        try:
            read_task = asyncio.create_task(
                asyncio.to_thread(
                    _read_regular_file, plan, _max_agent_file_bytes()
                )
            )
            try:
                file_data, _, read_error = await asyncio.shield(read_task)
            except asyncio.CancelledError:
                release_permit = False
                read_task.add_done_callback(lambda _task: semaphore.release())
                raise
            if read_error or file_data is None:
                return SendResult(
                    success=False, error="Local file delivery is not authorized"
                )
            msg_id = await self._upload_file(
                chat_id,
                file_data,
                requested_name,
                ct,
                caption,
                reply_to,
                metadata,
            )
            if not msg_id:
                return SendResult(success=False, error="File upload failed")
            return SendResult(success=True, message_id=msg_id)
        finally:
            if release_permit:
                semaphore.release()

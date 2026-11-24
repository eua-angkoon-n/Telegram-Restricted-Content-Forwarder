import asyncio
import json
import os
import re
import zlib
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from pyrogram import Client, enums, errors, raw, types

# Fixed on purpose: the library defaults embed the host kernel and library
# versions, so Telegram would see a "new device" after every move or upgrade.
DEVICE_MODEL = "Telegram Forwarder"
SYSTEM_VERSION = "Linux"
APP_VERSION = "1.0"
LANG_CODE = "en"

CAPTION_LIMIT = 1024
MAX_RECONNECT_DELAY = 300.0

# Telegram no longer accepts the stored authorization; only a new login helps.
REAUTH_ERRORS = (errors.Unauthorized, errors.AuthKeyDuplicated)
# Worth another attempt: the network or Telegram itself is briefly unavailable.
TRANSIENT_ERRORS = (OSError, errors.InternalServerError, errors.ServiceUnavailable)


def _utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _user_label(user) -> str:
    name = " ".join(
        part
        for part in (getattr(user, "first_name", None), getattr(user, "last_name", None))
        if part
    )
    username = getattr(user, "username", None)
    if name and username:
        return f"{name} (@{username})"
    return name or (f"@{username}" if username else "Deleted Account")


def _chat_label(chat, signature=None) -> str:
    username = getattr(chat, "username", None)
    label = getattr(chat, "title", None) or (
        f"@{username}" if username else str(getattr(chat, "id", ""))
    )
    return f"{label} ({signature})" if signature else label


def _origin_label(origin) -> str | None:
    if getattr(origin, "sender_user", None):
        return _user_label(origin.sender_user)
    if getattr(origin, "sender_user_name", None):
        return origin.sender_user_name
    chat = getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
    if chat:
        return _chat_label(chat, getattr(origin, "author_signature", None))
    return None


def sender_line(message) -> str | None:
    """Describe who posted a message and, for forwards, where it came from."""
    parts = []
    if getattr(message, "from_user", None):
        parts.append(f"👤 {_user_label(message.from_user)}")
    elif getattr(message, "sender_chat", None):
        signature = getattr(message, "author_signature", None)
        parts.append(f"👤 {_chat_label(message.sender_chat, signature)}")
    elif getattr(message, "author_signature", None):
        parts.append(f"👤 {message.author_signature}")

    origin = getattr(message, "forward_origin", None)
    origin_label = _origin_label(origin) if origin else None
    if origin_label:
        parts.append(f"↪ {origin_label}")
    return " · ".join(parts) or None


class ServiceError(RuntimeError):
    pass


@dataclass(frozen=True)
class TelegramSettings:
    data_dir: Path
    api_id: int | None
    api_hash: str | None
    proxies: tuple[str, ...] = ()
    poll_seconds: float = 5.0


@dataclass
class PendingLogin:
    client: Client
    code_hash: str


class TelegramService:
    def __init__(self, settings: TelegramSettings, client_factory=Client):
        self.settings = settings
        self.client_factory = client_factory
        self.accounts_dir = settings.data_dir / "account"
        self.config_dir = settings.data_dir / "data"
        self.downloads_dir = settings.data_dir / "downloads"
        for directory in (self.accounts_dir, self.config_dir, self.downloads_dir):
            directory.mkdir(parents=True, exist_ok=True)

        self.pending_logins: dict[str, PendingLogin] = {}
        # One connected client per account, shared by every operation, so a
        # session file is never opened by two connections at once.
        self.clients: dict[str, Client] = {}
        self.client_locks: dict[str, asyncio.Lock] = {}
        self.forward_phone: str | None = None
        self.logs = deque(maxlen=200)
        self.forward_task: asyncio.Task | None = None
        self.stop_event = asyncio.Event()
        self.forward_state = {
            "active": False,
            "status": "stopped",
            "success": 0,
            "failed": 0,
            "total": 0,
            "last_forwarded": None,
        }

    @staticmethod
    def _validate_phone(phone: str) -> str:
        if not re.fullmatch(r"\+[1-9]\d{3,14}", phone):
            raise ServiceError("Enter a valid international phone number.")
        return phone

    def _proxy(self, phone: str):
        if not self.settings.proxies:
            return None
        # Keep each account on one proxy: an auth key that hops between exit
        # addresses is what Telegram treats as a hijacked session.
        index = zlib.crc32(phone.encode()) % len(self.settings.proxies)
        parts = self.settings.proxies[index].split(":", 3)
        if len(parts) != 4 or not parts[1].isdigit():
            raise ServiceError("A configured proxy is invalid.")
        host, port, username, password = parts
        return {
            "scheme": "socks5",
            "hostname": host,
            "port": int(port),
            "username": username,
            "password": password,
        }

    def _new_client(self, phone: str):
        if not self.settings.api_id or not self.settings.api_hash:
            raise ServiceError(
                "TELEGRAM_API_ID and TELEGRAM_API_HASH must be configured."
            )
        return self.client_factory(
            name=phone,
            api_id=self.settings.api_id,
            api_hash=self.settings.api_hash,
            workdir=str(self.accounts_dir),
            proxy=self._proxy(phone),
            device_model=DEVICE_MODEL,
            system_version=SYSTEM_VERSION,
            app_version=APP_VERSION,
            lang_code=LANG_CODE,
        )

    async def _get_client(self, phone: str):
        lock = self.client_locks.setdefault(phone, asyncio.Lock())
        async with lock:
            client = self.clients.get(phone)
            if client is None:
                client = self._new_client(phone)
                await client.connect()
                self.clients[phone] = client
            return client

    async def _drop_client(self, phone: str) -> None:
        client = self.clients.pop(phone, None)
        if client is None:
            return
        try:
            await client.disconnect()
        except Exception:
            pass

    def _write_account_marker(self, phone: str) -> None:
        marker = self.config_dir / f"{phone}.json"
        marker.write_text(json.dumps({"phone": phone}), encoding="utf-8")

    async def _finish_login(self, phone: str):
        pending = self.pending_logins.pop(phone)
        try:
            user = await pending.client.get_me()
            self._write_account_marker(phone)
        except Exception as exc:
            try:
                await pending.client.disconnect()
            except Exception:
                pass
            raise ServiceError(str(exc)) from exc
        # Keep the signed-in connection instead of reconnecting for the next call.
        self.clients[phone] = pending.client
        return {
            "status": "signed_in",
            "phone": phone,
            "name": user.first_name or phone,
        }

    async def request_login_code(self, phone: str):
        phone = self._validate_phone(phone)
        if self.forward_state["active"] and self.forward_phone == phone:
            raise ServiceError(
                "Stop forwarding before signing in to this account again."
            )
        previous = self.pending_logins.pop(phone, None)
        if previous:
            await previous.client.disconnect()
        await self._drop_client(phone)

        client = self._new_client(phone)
        try:
            await client.connect()
            result = await client.send_code(phone)
        except Exception as exc:
            try:
                await client.disconnect()
            except Exception:
                pass
            raise ServiceError(str(exc)) from exc

        self.pending_logins[phone] = PendingLogin(client, result.phone_code_hash)
        return {"status": "code_required", "phone": phone}

    async def verify_login_code(self, phone: str, code: str):
        phone = self._validate_phone(phone)
        pending = self.pending_logins.get(phone)
        if pending is None:
            raise ServiceError("Request a new login code first.")
        try:
            await pending.client.sign_in(phone, pending.code_hash, code)
        except errors.SessionPasswordNeeded:
            return {"status": "password_required", "phone": phone}
        except Exception as exc:
            raise ServiceError(str(exc)) from exc
        return await self._finish_login(phone)

    async def verify_login_password(self, phone: str, password: str):
        phone = self._validate_phone(phone)
        pending = self.pending_logins.get(phone)
        if pending is None:
            raise ServiceError("Request a new login code first.")
        try:
            await pending.client.check_password(password=password)
        except Exception as exc:
            raise ServiceError(str(exc)) from exc
        return await self._finish_login(phone)

    def _list_accounts(self):
        sessions = {path.stem for path in self.accounts_dir.glob("*.session")}
        markers = {path.stem for path in self.config_dir.glob("*.json")}
        return sorted(sessions & markers)

    async def snapshot(self):
        return {
            "accounts": self._list_accounts(),
            "forwarding": {**self.forward_state, "logs": list(self.logs)},
        }

    def _add_log(self, level: str, message: str) -> None:
        self.logs.appendleft(
            {
                "time": datetime.now(timezone.utc).isoformat(),
                "level": level,
                "message": message,
            }
        )

    async def remove_account(self, phone: str) -> None:
        phone = self._validate_phone(phone)
        if self.forward_state["active"]:
            raise ServiceError("Stop forwarding before removing an account.")
        if phone in self._list_accounts():
            # End the authorization on Telegram's side too, so a removed
            # account does not linger in the user's active sessions.
            try:
                client = await self._get_client(phone)
                await client.invoke(raw.functions.auth.LogOut())
            except Exception:
                pass
        await self._drop_client(phone)
        for path in (
            self.accounts_dir / f"{phone}.session",
            self.accounts_dir / f"{phone}.session-journal",
            self.config_dir / f"{phone}.json",
        ):
            path.unlink(missing_ok=True)

    async def list_chats(self, phone: str):
        phone = self._validate_phone(phone)
        if phone not in self._list_accounts():
            raise ServiceError("Account not found.")
        chats = []
        try:
            client = await self._get_client(phone)
            async for dialog in client.get_dialogs():
                chat = dialog.chat
                if chat.type in (
                    enums.ChatType.GROUP,
                    enums.ChatType.SUPERGROUP,
                    enums.ChatType.CHANNEL,
                ):
                    chats.append({"id": chat.id, "title": chat.title or str(chat.id)})
        except REAUTH_ERRORS as exc:
            await self._drop_client(phone)
            raise ServiceError(
                "Telegram ended this session. Sign in to the account again."
            ) from exc
        except Exception as exc:
            raise ServiceError(str(exc)) from exc
        return sorted(chats, key=lambda item: item["title"].casefold())

    @staticmethod
    def _parse_chat_target(value: str):
        value = value.strip()
        if re.fullmatch(r"-?\d+", value):
            return int(value)
        if re.fullmatch(r"@[A-Za-z0-9_]{5,}", value):
            return value
        if re.fullmatch(r"(?:https?://)?t\.me/\+[\w-]{10,}", value):
            return value.removeprefix("https://").removeprefix("http://")
        raise ServiceError("Use a channel @username, invite link, or numeric chat ID.")

    async def _resolve_chat(self, client, target):
        if isinstance(target, int):
            return await client.get_chat(target)
        try:
            return await client.join_chat(target)
        except errors.UserAlreadyParticipant:
            return await client.get_chat(target)

    async def start_forwarding(self, phone: str, source: str, destination: str):
        phone = self._validate_phone(phone)
        if phone not in self._list_accounts():
            raise ServiceError("Account not found.")
        if self.forward_task and not self.forward_task.done():
            raise ServiceError("A forwarding job is already active.")

        source_target = self._parse_chat_target(source)
        destination_target = self._parse_chat_target(destination)
        self.stop_event = asyncio.Event()
        self.forward_phone = phone
        self.logs.clear()
        self.forward_state.update(
            active=True,
            status="starting",
            success=0,
            failed=0,
            total=0,
            last_forwarded=None,
        )
        self._add_log("info", "Connecting to Telegram…")
        self.forward_task = asyncio.create_task(
            self._run_forwarding(phone, source_target, destination_target)
        )
        return {"status": "starting"}

    async def stop_forwarding(self):
        task = self.forward_task
        if not task or task.done():
            self.forward_state.update(active=False, status="stopped")
            return {"status": "stopped"}
        self.forward_state["status"] = "stopping"
        self.stop_event.set()
        await task
        return {"status": "stopped"}

    async def _wait(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def _run_forwarding(self, phone: str, source, destination) -> None:
        source_chat = destination_chat = last_id = None
        failures = 0
        try:
            while not self.stop_event.is_set():
                try:
                    client = await self._get_client(phone)
                    if last_id is None:
                        source_chat = await self._resolve_chat(client, source)
                        destination_chat = await self._resolve_chat(client, destination)
                        newest = 0
                        async for message in client.get_chat_history(
                            source_chat.id, limit=1
                        ):
                            newest = message.id
                        last_id = newest
                        self._add_log(
                            "success", "Forwarding is active and watching for new media."
                        )

                    new_messages = []
                    async for message in client.get_chat_history(
                        source_chat.id, limit=0
                    ):
                        if message.id <= last_id:
                            break
                        new_messages.append(message)
                    self.forward_state["status"] = "running"
                    failures = 0

                    for message in reversed(new_messages):
                        if self.stop_event.is_set():
                            break
                        if not await self._process_message(
                            client, destination_chat.id, message
                        ):
                            break
                        last_id = message.id
                except REAUTH_ERRORS as exc:
                    await self._drop_client(phone)
                    self.forward_state["status"] = "reauth_required"
                    self._add_log(
                        "error",
                        f"Telegram ended this session ({exc}). "
                        "Sign in to the account again.",
                    )
                    return
                except errors.FloodWait as exc:
                    self._add_log(
                        "warning", f"Telegram rate limit: waiting {exc.value}s."
                    )
                    await self._wait(exc.value)
                    continue
                except TRANSIENT_ERRORS as exc:
                    # Reconnect on the same session file; the stored
                    # authorization is still valid, only the link is gone.
                    failures += 1
                    await self._drop_client(phone)
                    delay = min(
                        self.settings.poll_seconds * 2**failures, MAX_RECONNECT_DELAY
                    )
                    self.forward_state["status"] = "reconnecting"
                    self._add_log(
                        "warning", f"Connection problem ({exc}); retrying in {delay:g}s."
                    )
                    await self._wait(delay)
                    continue

                await self._wait(self.settings.poll_seconds)
        except Exception as exc:
            self.forward_state["status"] = "error"
            self._add_log("error", f"Forwarding stopped: {exc}")
        finally:
            self.forward_state["active"] = False
            if self.forward_state["status"] not in ("error", "reauth_required"):
                self.forward_state["status"] = "stopped"
                self._add_log("info", "Forwarding stopped.")

    @staticmethod
    def _caption_with_sender(caption, line):
        """Return the caption to send and the sender line left over, if any."""
        if not line:
            return caption, None
        if not caption:
            return line, None
        combined = f"{caption}\n\n{line}"
        if _utf16_length(combined) > CAPTION_LIMIT:
            return caption, line
        return combined, None

    async def _send_sender_line(self, client, destination_id: int, sent, line) -> None:
        try:
            kwargs = {"chat_id": destination_id, "text": line}
            if getattr(sent, "id", None):
                kwargs["reply_parameters"] = types.ReplyParameters(message_id=sent.id)
            await client.send_message(**kwargs)
        except Exception as exc:
            self._add_log("warning", f"Sender name could not be added: {exc}")

    async def _process_message(self, client, destination_id: int, message) -> bool:
        path = None
        sender = None
        try:
            download_dir = f"{self.downloads_dir}{os.sep}"
            # Appended, never prepended: caption_entities keep their offsets.
            caption, follow_up = self._caption_with_sender(
                message.caption, sender_line(message)
            )
            if message.photo:
                path = await message.download(file_name=download_dir)
                sender = client.send_photo
                kwargs = {
                    "photo": path,
                    "caption": caption,
                    "caption_entities": message.caption_entities,
                }
            elif message.video:
                path = await message.download(file_name=download_dir)
                sender = client.send_video
                kwargs = {
                    "video": path,
                    "caption": caption,
                    "caption_entities": message.caption_entities,
                    "duration": message.video.duration,
                    "width": message.video.width,
                    "height": message.video.height,
                }
            elif message.animation:
                path = await message.download(file_name=download_dir)
                sender = client.send_animation
                kwargs = {
                    "animation": path,
                    "caption": caption,
                    "caption_entities": message.caption_entities,
                    "duration": message.animation.duration,
                    "width": message.animation.width,
                    "height": message.animation.height,
                }
            elif message.video_note:
                path = await message.download(file_name=download_dir)
                sender = client.send_video_note
                kwargs = {
                    "video_note": path,
                    "duration": message.video_note.duration,
                    "length": message.video_note.length,
                }
                # Video notes cannot carry a caption.
                follow_up = sender_line(message)
            else:
                return True

            self.forward_state["total"] += 1
            sent = await sender(chat_id=destination_id, **kwargs)
            if follow_up:
                await self._send_sender_line(client, destination_id, sent, follow_up)
            self.forward_state["success"] += 1
            self.forward_state["last_forwarded"] = message.id
            self._add_log("success", f"Message {message.id} forwarded.")
            return True
        except errors.FloodWait as exc:
            self.forward_state["failed"] += 1
            self._add_log("warning", f"Telegram rate limit: waiting {exc.value}s.")
            await self._wait(exc.value)
            return False
        except REAUTH_ERRORS:
            # The session, not the message, is the problem: the worker loop
            # reports it and asks for a new login.
            raise
        except Exception as exc:
            self.forward_state["failed"] += 1
            self._add_log("error", f"Message {message.id} failed: {exc}")
            return False
        finally:
            if path:
                Path(path).unlink(missing_ok=True)

    async def shutdown(self) -> None:
        if self.forward_task and not self.forward_task.done():
            self.stop_event.set()
            await self.forward_task
        for pending in list(self.pending_logins.values()):
            try:
                await pending.client.disconnect()
            except Exception:
                pass
        self.pending_logins.clear()
        for phone in list(self.clients):
            await self._drop_client(phone)

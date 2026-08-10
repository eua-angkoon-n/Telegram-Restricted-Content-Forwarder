import asyncio
import json
import os
import random
import re
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from pyrogram import Client, enums, errors


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

    def _proxy(self):
        if not self.settings.proxies:
            return None
        parts = random.choice(self.settings.proxies).split(":", 3)
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
            proxy=self._proxy(),
        )

    def _write_account_marker(self, phone: str) -> None:
        marker = self.config_dir / f"{phone}.json"
        marker.write_text(json.dumps({"phone": phone}), encoding="utf-8")

    async def _finish_login(self, phone: str):
        pending = self.pending_logins[phone]
        try:
            user = await pending.client.get_me()
            self._write_account_marker(phone)
            return {
                "status": "signed_in",
                "phone": phone,
                "name": user.first_name or phone,
            }
        except Exception as exc:
            raise ServiceError(str(exc)) from exc
        finally:
            self.pending_logins.pop(phone, None)
            try:
                await pending.client.disconnect()
            except Exception:
                pass

    async def request_login_code(self, phone: str):
        phone = self._validate_phone(phone)
        previous = self.pending_logins.pop(phone, None)
        if previous:
            await previous.client.disconnect()

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
        client = self._new_client(phone)
        chats = []
        try:
            await client.connect()
            async for dialog in client.get_dialogs():
                chat = dialog.chat
                if chat.type in (
                    enums.ChatType.GROUP,
                    enums.ChatType.SUPERGROUP,
                    enums.ChatType.CHANNEL,
                ):
                    chats.append({"id": chat.id, "title": chat.title or str(chat.id)})
        except Exception as exc:
            raise ServiceError(str(exc)) from exc
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
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

    async def _run_forwarding(self, phone: str, source, destination) -> None:
        client = self._new_client(phone)
        try:
            await client.connect()
            source_chat = await self._resolve_chat(client, source)
            destination_chat = await self._resolve_chat(client, destination)

            last_id = 0
            async for message in client.get_chat_history(source_chat.id, limit=1):
                last_id = message.id

            self.forward_state["status"] = "running"
            self._add_log("success", "Forwarding is active and watching for new media.")
            while not self.stop_event.is_set():
                new_messages = []
                async for message in client.get_chat_history(source_chat.id, limit=0):
                    if message.id <= last_id:
                        break
                    new_messages.append(message)

                for message in reversed(new_messages):
                    if self.stop_event.is_set():
                        break
                    if not await self._process_message(
                        client, destination_chat.id, message
                    ):
                        break
                    last_id = message.id

                try:
                    await asyncio.wait_for(
                        self.stop_event.wait(), timeout=self.settings.poll_seconds
                    )
                except TimeoutError:
                    pass
        except Exception as exc:
            self.forward_state["status"] = "error"
            self._add_log("error", f"Forwarding stopped: {exc}")
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
            self.forward_state["active"] = False
            if self.forward_state["status"] != "error":
                self.forward_state["status"] = "stopped"
                self._add_log("info", "Forwarding stopped.")

    async def _process_message(self, client, destination_id: int, message) -> bool:
        path = None
        sender = None
        try:
            download_dir = f"{self.downloads_dir}{os.sep}"
            if message.photo:
                path = await message.download(file_name=download_dir)
                sender = client.send_photo
                kwargs = {
                    "photo": path,
                    "caption": message.caption,
                    "caption_entities": message.caption_entities,
                }
            elif message.video:
                path = await message.download(file_name=download_dir)
                sender = client.send_video
                kwargs = {
                    "video": path,
                    "caption": message.caption,
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
                    "caption": message.caption,
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
            else:
                return True

            self.forward_state["total"] += 1
            await sender(chat_id=destination_id, **kwargs)
            self.forward_state["success"] += 1
            self.forward_state["last_forwarded"] = message.id
            self._add_log("success", f"Message {message.id} forwarded.")
            return True
        except errors.FloodWait as exc:
            self.forward_state["failed"] += 1
            self._add_log("warning", f"Telegram rate limit: waiting {exc.value}s.")
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=exc.value)
            except TimeoutError:
                pass
            return False
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

import asyncio
import json
import os
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from pyrogram import errors, types

from webapp.telegram_service import (
    MAX_RECONNECT_DELAY,
    REAUTH_ERRORS,
    TRANSIENT_ERRORS,
    ServiceError,
    is_video_file,
    sender_line,
)

# Telegram returns at most this many history messages per request.
BATCH_SIZE = 100
# How often one message or album is tried before it is skipped.
MAX_ATTEMPTS = 3

ROUTE_KEYS = ("phone", "source", "destination")


def _has_media(message) -> bool:
    return bool(
        message.photo
        or message.video
        or message.animation
        or message.video_note
        or is_video_file(message)
    )


def _label(unit) -> str:
    if len(unit) == 1:
        return f"Message {unit[0].id}"
    return f"Album {unit[0].id}–{unit[-1].id}"


class BackfillJob:
    """Copy the media already in a chat, oldest first, then finish.

    It runs beside the live forwarding job and keeps its own state, logs and
    checkpoint, so neither job changes how the other behaves.
    """

    def __init__(self, service):
        self.service = service
        self.job_file = service.settings.data_dir / "backfill_job.json"
        # Its own folder, so the two jobs never touch each other's files.
        self.downloads_dir = service.downloads_dir / "backfill"

        self.logs = deque(maxlen=200)
        self.task: asyncio.Task | None = None
        self.stop_event = asyncio.Event()
        self.state = {
            "active": False,
            "status": "stopped",
            "success": 0,
            "failed": 0,
            "total": 0,
            "last_id": None,
            "end_id": None,
        }
        # The route and how far it got; kept after a stop so it can carry on.
        self.job = self._load() or {}
        self.state.update(
            last_id=self.job.get("last_id"), end_id=self.job.get("end_id")
        )

    def uses(self, phone: str) -> bool:
        return self.state["active"] and self.job.get("phone") == phone

    def snapshot(self):
        saved = {key: self.job[key] for key in ROUTE_KEYS} if self.job else None
        return {**self.state, "saved": saved, "logs": list(self.logs)}

    def _add_log(self, level: str, message: str) -> None:
        self.logs.appendleft(
            {
                "time": datetime.now(timezone.utc).isoformat(),
                "level": level,
                "message": message,
            }
        )

    def _load(self):
        try:
            job = json.loads(self.job_file.read_text(encoding="utf-8"))
            if not all(isinstance(job[key], str) for key in ROUTE_KEYS):
                raise ValueError("incomplete route")
            job["last_id"] = int(job.get("last_id") or 0)
            if job.get("end_id") is not None:
                job["end_id"] = int(job["end_id"])
            return job
        except FileNotFoundError:
            return None
        except Exception:
            self.job_file.unlink(missing_ok=True)
            return None

    def _save(self) -> None:
        temporary = self.job_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.job), encoding="utf-8")
        temporary.replace(self.job_file)

    def _advance(self, last_id: int) -> None:
        self.job["last_id"] = last_id
        self.state["last_id"] = last_id
        self._save()

    async def start(
        self, phone: str, source: str, destination: str, restart: bool = False
    ):
        service = self.service
        phone = service._validate_phone(phone)
        if phone not in service._list_accounts():
            raise ServiceError("Account not found.")
        if self.task and not self.task.done():
            raise ServiceError("A history job is already active.")
        service._parse_chat_target(source)
        service._parse_chat_target(destination)

        route = {"phone": phone, "source": source, "destination": destination}
        saved = self._load()
        same_route = saved is not None and all(
            saved[key] == route[key] for key in ROUTE_KEYS
        )
        if restart or not same_route:
            saved = {**route, "last_id": 0, "end_id": None}
        self.job = {**saved, "running": True}
        self._save()
        self._launch()
        if self.job["last_id"]:
            self._add_log(
                "info", f"Carrying on after message {self.job['last_id']}."
            )
        return {"status": "starting"}

    async def resume(self) -> None:
        """Carry on with the job that was running when the service stopped."""
        job = self._load()
        if not job or not job.get("running"):
            return
        try:
            self.service._parse_chat_target(job["source"])
            self.service._parse_chat_target(job["destination"])
            if job["phone"] not in self.service._list_accounts():
                raise ServiceError("Account not found.")
        except ServiceError:
            self.job_file.unlink(missing_ok=True)
            self.job = {}
            return
        self.job = job
        self._launch()
        self._add_log("info", "Resuming the history job after a restart.")

    def _launch(self) -> None:
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        self.stop_event = asyncio.Event()
        self.logs.clear()
        self.state.update(
            active=True,
            status="starting",
            success=0,
            failed=0,
            total=0,
            last_id=self.job["last_id"],
            end_id=self.job["end_id"],
        )
        self._add_log("info", "Connecting to Telegram…")
        self.task = asyncio.create_task(self._run())

    async def stop(self):
        task = self.task
        if not task or task.done():
            return {"status": self.state["status"]}
        # The checkpoint stays, so the same route can carry on later; only
        # the flag that makes a restart resume it is cleared.
        self.job["running"] = False
        self._save()
        self.state["status"] = "stopping"
        self.stop_event.set()
        await task
        return {"status": self.state["status"]}

    async def shutdown(self) -> None:
        if self.task and not self.task.done():
            self.stop_event.set()
            await self.task

    async def _wait(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def _run(self) -> None:
        service = self.service
        job = self.job
        phone = job["phone"]
        source = service._parse_chat_target(job["source"])
        destination = service._parse_chat_target(job["destination"])
        source_chat = destination_chat = None
        client = None
        failures = 0
        completed = False
        try:
            while not self.stop_event.is_set():
                try:
                    client = await service._get_client(phone)
                    if source_chat is None:
                        source_chat = await service._resolve_chat(client, source)
                        destination_chat = await service._resolve_chat(
                            client, destination
                        )
                        if job["end_id"] is None:
                            # Fixed once: whatever is posted later is not
                            # history and is left to the live job.
                            newest = 0
                            async for message in client.get_chat_history(
                                source_chat.id, limit=1
                            ):
                                newest = message.id
                            job["end_id"] = newest
                            self._save()
                        self.state["end_id"] = job["end_id"]
                        self._add_log(
                            "success",
                            f"Copying history up to message {job['end_id']}.",
                        )

                    self.state["status"] = "running"
                    batch = []
                    if job["last_id"] < job["end_id"]:
                        batch = await self._next_batch(client, source_chat.id)
                    failures = 0
                    if not batch:
                        completed = True
                        break

                    for unit in self._units(batch):
                        if self.stop_event.is_set():
                            break
                        if not await self._forward_unit(
                            client, destination_chat.id, unit
                        ):
                            break
                        self._advance(unit[-1].id)
                except REAUTH_ERRORS as exc:
                    await service._drop_client(phone)
                    self.state["status"] = "reauth_required"
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
                except TRANSIENT_ERRORS as exc:
                    failures += 1
                    # The live job shares this client and may already have
                    # replaced it; only a connection that is still the
                    # current one is dropped.
                    if service.clients.get(phone) is client:
                        await service._drop_client(phone)
                    delay = min(
                        service.settings.poll_seconds * 2**failures,
                        MAX_RECONNECT_DELAY,
                    )
                    self.state["status"] = "reconnecting"
                    self._add_log(
                        "warning", f"Connection problem ({exc}); retrying in {delay:g}s."
                    )
                    await self._wait(delay)
        except Exception as exc:
            self.state["status"] = "error"
            self._add_log("error", f"History job stopped: {exc}")
        finally:
            self.state["active"] = False
            if completed:
                self.state.update(status="completed", last_id=job["end_id"])
                self.job_file.unlink(missing_ok=True)
                self.job = {}
                self._add_log("success", "History copied. The job is complete.")
            elif self.state["status"] in ("error", "reauth_required"):
                # Not resumed by a restart, but the checkpoint stays so the
                # route can carry on once the cause is fixed.
                job["running"] = False
                self._save()
            else:
                self.state["status"] = "stopped"
                self._add_log(
                    "info", f"History job stopped after message {job['last_id']}."
                )

    async def _next_batch(self, client, chat_id: int):
        last_id, end_id = self.job["last_id"], self.job["end_id"]
        messages = []
        async for message in client.get_chat_history(
            chat_id,
            limit=BATCH_SIZE,
            min_id=last_id + 1,
            max_id=end_id,
            reverse=True,
        ):
            messages.append(message)
        return sorted(
            (message for message in messages if last_id < message.id <= end_id),
            key=lambda message: message.id,
        )

    def _units(self, batch):
        """Split a batch into single messages and whole albums."""
        units = []
        for message in batch:
            group = getattr(message, "media_group_id", None)
            if (
                group is not None
                and units
                and getattr(units[-1][0], "media_group_id", None) == group
            ):
                units[-1].append(message)
            else:
                units.append([message])
        # The rest of a trailing album may be in the next batch: leave it
        # for that one so the album is sent whole.
        if (
            len(units) > 1
            and getattr(units[-1][0], "media_group_id", None) is not None
            and batch[-1].id < self.job["end_id"]
        ):
            units.pop()
        return units

    async def _forward_unit(self, client, destination_id: int, unit) -> bool:
        """Send one message or album; False means a stop interrupted it."""
        pending = [message for message in unit if _has_media(message)]
        if not pending:
            return True
        # What already went out is not sent again by the next attempt.
        sent_ids: set[int] = set()
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                await self._send_unit(client, destination_id, pending, sent_ids)
                self._add_log("success", f"{_label(unit)} copied.")
                return True
            except (errors.FloodWait, *REAUTH_ERRORS):
                # Neither is the message's fault: the worker loop waits or
                # asks for a new login.
                raise
            except Exception as exc:
                if attempt < MAX_ATTEMPTS:
                    self._add_log(
                        "warning", f"{_label(unit)} failed ({exc}); trying again."
                    )
                    await self._wait(self.service.settings.poll_seconds * 2**attempt)
                    if self.stop_event.is_set():
                        return False
                    continue
                try:
                    await client.get_me()
                except REAUTH_ERRORS:
                    raise
                except Exception as probe:
                    # The connection is gone, so the failure says nothing
                    # about the message: reconnect instead of skipping it.
                    raise ConnectionError(str(probe) or str(exc)) from exc
                skipped = len(pending) - len(sent_ids)
                self.state["failed"] += skipped
                self.state["total"] += skipped
                self._add_log(
                    "error",
                    f"{_label(unit)} skipped after {MAX_ATTEMPTS} attempts: {exc}",
                )
        return True

    async def _send_unit(self, client, destination_id: int, pending, sent_ids) -> None:
        remaining = [message for message in pending if message.id not in sent_ids]
        album = [message for message in remaining if message.photo or message.video]
        if len(album) > 1:
            await self._send_album(client, destination_id, album)
            self._mark_sent(album, sent_ids)
        for message in remaining:
            if message.id not in sent_ids:
                await self._send_single(client, destination_id, message)
                self._mark_sent([message], sent_ids)

    def _mark_sent(self, messages, sent_ids) -> None:
        sent_ids.update(message.id for message in messages)
        self.state["success"] += len(messages)
        self.state["total"] += len(messages)

    async def _send_single(self, client, destination_id: int, message) -> None:
        path = None
        try:
            sender, kwargs, path, follow_up = await self.service._prepare_upload(
                client, message, f"{self.downloads_dir}{os.sep}", documents=True
            )
            sent = await sender(chat_id=destination_id, **kwargs)
            if follow_up:
                await self.service._send_sender_line(
                    client, destination_id, sent, follow_up
                )
        finally:
            if path:
                Path(path).unlink(missing_ok=True)

    async def _send_album(self, client, destination_id: int, album) -> None:
        # The sender is named once, on the item that carries the caption.
        captioned = next((message for message in album if message.caption), album[0])
        paths = []
        try:
            media = []
            follow_up = None
            for message in album:
                caption = message.caption
                if message is captioned:
                    caption, follow_up = self.service._caption_with_sender(
                        caption, sender_line(message)
                    )
                path = await message.download(
                    file_name=f"{self.downloads_dir}{os.sep}"
                )
                paths.append(path)
                if message.photo:
                    media.append(
                        types.InputMediaPhoto(
                            path,
                            caption=caption or "",
                            caption_entities=message.caption_entities,
                        )
                    )
                else:
                    media.append(
                        types.InputMediaVideo(
                            path,
                            caption=caption or "",
                            caption_entities=message.caption_entities,
                            duration=message.video.duration,
                            width=message.video.width,
                            height=message.video.height,
                        )
                    )
            sent = await client.send_media_group(chat_id=destination_id, media=media)
            if follow_up:
                await self.service._send_sender_line(
                    client, destination_id, sent[0] if sent else None, follow_up
                )
        finally:
            for path in paths:
                Path(path).unlink(missing_ok=True)

import asyncio
import json
from pathlib import Path

import pytest
from pyrogram import errors

import webapp.backfill
from webapp.telegram_service import ServiceError, TelegramService, TelegramSettings

PHONE = "+15551234567"


class FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id


class FakeVideo:
    duration = 4
    width = 640
    height = 360


class FakeDocument:
    def __init__(self, mime_type, file_name):
        self.mime_type = mime_type
        self.file_name = file_name


class FakeMessage:
    photo = None
    video = None
    animation = None
    video_note = None
    document = None
    caption = None
    caption_entities = None
    media_group_id = None

    def __init__(self, message_id, **fields):
        self.id = message_id
        self.__dict__.update(fields)

    async def download(self, file_name):
        path = Path(file_name) / f"{self.id}.bin"
        path.write_bytes(b"media")
        return str(path)


def photo(message_id, **fields):
    return FakeMessage(message_id, photo=True, **fields)


def video(message_id, **fields):
    return FakeMessage(message_id, video=FakeVideo(), **fields)


def text(message_id):
    return FakeMessage(message_id)


def sent_id(path):
    return int(Path(path).stem)


class FakeHistoryClient:
    def __init__(self, messages):
        self.messages = sorted(messages, key=lambda message: message.id)
        self.sent = []
        self.albums = []
        self.documents = []

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def get_me(self):
        return object()

    async def get_chat(self, chat_id):
        return FakeChat(chat_id)

    async def get_chat_history(
        self, chat_id, limit=0, min_id=0, max_id=0, reverse=False
    ):
        chosen = [
            message
            for message in self.messages
            if message.id >= min_id and (not max_id or message.id <= max_id)
        ]
        if not reverse:
            chosen.reverse()
        for message in chosen[: limit or None]:
            yield message

    async def send_photo(self, **kwargs):
        self.sent.append(sent_id(kwargs["photo"]))

    async def send_video(self, **kwargs):
        self.sent.append(sent_id(kwargs["video"]))

    async def send_document(self, **kwargs):
        self.documents.append(kwargs)
        self.sent.append(sent_id(kwargs["document"]))

    async def send_media_group(self, chat_id, media):
        self.albums.append([sent_id(item.media) for item in media])
        return []


class FakeFloodAtClient(FakeHistoryClient):
    """Hits a long rate limit at one message, so a job can be stopped there."""

    flood_at = 2

    async def send_photo(self, **kwargs):
        if sent_id(kwargs["photo"]) == self.flood_at:
            raise errors.FloodWait(3600)
        await super().send_photo(**kwargs)


def save_account(tmp_path, phone=PHONE):
    account_dir = tmp_path / "account"
    config_dir = tmp_path / "data"
    account_dir.mkdir(exist_ok=True)
    config_dir.mkdir(exist_ok=True)
    (account_dir / f"{phone}.session").touch()
    (config_dir / f"{phone}.json").write_text(json.dumps({"phone": phone}))


def make_service(tmp_path, client_factory):
    settings = TelegramSettings(
        data_dir=tmp_path,
        api_id=12345,
        api_hash="api-hash",
        proxies=(),
        poll_seconds=0.01,
    )
    return TelegramService(settings, client_factory=client_factory)


async def wait_for(condition):
    for _ in range(300):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not reached")


async def run_to_end(service):
    await service.backfill.start(PHONE, "-1001", "-1002")
    await wait_for(lambda: not service.backfill.state["active"])
    return (await service.snapshot())["backfill"]


@pytest.mark.asyncio
async def test_history_is_copied_oldest_first_and_the_job_completes(tmp_path):
    save_account(tmp_path)
    client = FakeHistoryClient([photo(3), text(2), video(1), photo(5), text(6)])
    service = make_service(tmp_path, lambda **_: client)

    state = await run_to_end(service)

    assert client.sent == [1, 3, 5]
    assert state["status"] == "completed"
    assert (state["success"], state["failed"], state["total"]) == (3, 0, 3)
    assert state["last_id"] == state["end_id"] == 6
    assert not service.backfill.job_file.exists()
    assert list((tmp_path / "downloads" / "backfill").iterdir()) == []


@pytest.mark.asyncio
async def test_history_is_read_in_batches(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp.backfill, "BATCH_SIZE", 2)
    save_account(tmp_path)
    client = FakeHistoryClient([photo(number) for number in range(1, 8)])
    service = make_service(tmp_path, lambda **_: client)

    state = await run_to_end(service)

    assert client.sent == [1, 2, 3, 4, 5, 6, 7]
    assert state["status"] == "completed"


@pytest.mark.asyncio
async def test_messages_posted_after_the_start_are_left_out(tmp_path):
    save_account(tmp_path)

    class GrowingClient(FakeHistoryClient):
        async def send_photo(self, **kwargs):
            await super().send_photo(**kwargs)
            if len(self.messages) == 2:
                self.messages.append(photo(3))

    client = GrowingClient([photo(1), photo(2)])
    service = make_service(tmp_path, lambda **_: client)

    state = await run_to_end(service)

    assert client.sent == [1, 2]
    assert state["status"] == "completed"


@pytest.mark.asyncio
async def test_a_message_that_keeps_failing_is_skipped(tmp_path):
    save_account(tmp_path)

    class BrokenMessage(FakeMessage):
        attempts = 0

        async def download(self, file_name):
            BrokenMessage.attempts += 1
            raise ValueError("file reference expired")

    client = FakeHistoryClient([photo(1), BrokenMessage(2, photo=True), photo(3)])
    service = make_service(tmp_path, lambda **_: client)

    state = await run_to_end(service)

    assert BrokenMessage.attempts == webapp.backfill.MAX_ATTEMPTS
    assert client.sent == [1, 3]
    assert state["status"] == "completed"
    assert (state["success"], state["failed"], state["total"]) == (2, 1, 3)
    assert any("Message 2 skipped" in entry["message"] for entry in state["logs"])


@pytest.mark.asyncio
async def test_a_lost_connection_does_not_skip_messages(tmp_path):
    save_account(tmp_path)

    class DeadClient(FakeHistoryClient):
        async def send_photo(self, **kwargs):
            raise ConnectionResetError("connection lost")

        async def get_me(self):
            raise ConnectionResetError("connection lost")

    created = []

    def factory(**_):
        kind = FakeHistoryClient if created else DeadClient
        created.append(kind([photo(1), photo(2)]))
        return created[-1]

    service = make_service(tmp_path, factory)

    state = await run_to_end(service)

    assert len(created) == 2
    assert created[1].sent == [1, 2]
    assert state["status"] == "completed"
    assert state["failed"] == 0


@pytest.mark.asyncio
async def test_an_album_is_sent_as_one_group(tmp_path):
    save_account(tmp_path)
    client = FakeHistoryClient(
        [
            photo(1),
            photo(2, media_group_id="a", caption="Trip"),
            video(3, media_group_id="a"),
            photo(4, media_group_id="a"),
            photo(5),
        ]
    )
    service = make_service(tmp_path, lambda **_: client)

    state = await run_to_end(service)

    assert client.albums == [[2, 3, 4]]
    assert client.sent == [1, 5]
    assert (state["success"], state["total"]) == (5, 5)
    assert list((tmp_path / "downloads" / "backfill").iterdir()) == []


@pytest.mark.asyncio
async def test_an_album_split_across_two_batches_stays_whole(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp.backfill, "BATCH_SIZE", 3)
    save_account(tmp_path)
    client = FakeHistoryClient(
        [
            photo(1),
            photo(2),
            photo(3, media_group_id="a"),
            photo(4, media_group_id="a"),
            photo(5),
        ]
    )
    service = make_service(tmp_path, lambda **_: client)

    await run_to_end(service)

    assert client.albums == [[3, 4]]
    assert client.sent == [1, 2, 5]


@pytest.mark.asyncio
async def test_a_video_sent_as_a_file_is_copied_as_a_file(tmp_path):
    save_account(tmp_path)
    client = FakeHistoryClient(
        [
            FakeMessage(1, document=FakeDocument("video/mp4", "clip.mp4")),
            FakeMessage(2, document=FakeDocument("application/pdf", "notes.pdf")),
        ]
    )
    service = make_service(tmp_path, lambda **_: client)

    state = await run_to_end(service)

    assert client.sent == [1]
    assert client.documents[0]["file_name"] == "clip.mp4"
    assert client.documents[0]["chat_id"] == -1002
    assert state["total"] == 1


@pytest.mark.asyncio
async def test_a_stopped_route_carries_on_where_it_left_off(tmp_path):
    save_account(tmp_path)
    messages = [photo(1), photo(2), photo(3)]
    first = FakeFloodAtClient(messages)
    service = make_service(tmp_path, lambda **_: first)

    await service.backfill.start(PHONE, "-1001", "-1002")
    await wait_for(lambda: first.sent == [1])
    await asyncio.wait_for(service.backfill.stop(), timeout=0.5)

    assert service.backfill.state["status"] == "stopped"
    assert json.loads(service.backfill.job_file.read_text())["last_id"] == 1

    second = FakeHistoryClient(messages)
    service.clients[PHONE] = second
    state = await run_to_end(service)

    assert second.sent == [2, 3]
    assert state["status"] == "completed"


@pytest.mark.asyncio
async def test_starting_over_copies_from_the_first_message(tmp_path):
    save_account(tmp_path)
    messages = [photo(1), photo(2), photo(3)]
    first = FakeFloodAtClient(messages)
    service = make_service(tmp_path, lambda **_: first)

    await service.backfill.start(PHONE, "-1001", "-1002")
    await wait_for(lambda: first.sent == [1])
    await service.backfill.stop()

    second = FakeHistoryClient(messages)
    service.clients[PHONE] = second
    await service.backfill.start(PHONE, "-1001", "-1002", restart=True)
    await wait_for(lambda: not service.backfill.state["active"])

    assert second.sent == [1, 2, 3]


@pytest.mark.asyncio
async def test_history_job_resumes_after_a_restart(tmp_path):
    save_account(tmp_path)
    messages = [photo(1), photo(2), photo(3)]
    client = FakeFloodAtClient(messages)
    first = make_service(tmp_path, lambda **_: client)

    await first.backfill.start(PHONE, "-1001", "-1002")
    await wait_for(lambda: client.sent == [1])
    await first.shutdown()

    resumed = FakeHistoryClient(messages)
    second = make_service(tmp_path, lambda **_: resumed)
    await second.backfill.resume()
    await wait_for(lambda: not second.backfill.state["active"])

    assert resumed.sent == [2, 3]
    assert second.backfill.state["status"] == "completed"
    await second.shutdown()


@pytest.mark.asyncio
async def test_stopped_history_job_is_not_resumed(tmp_path):
    save_account(tmp_path)
    client = FakeFloodAtClient([photo(1), photo(2)])
    first = make_service(tmp_path, lambda **_: client)

    await first.backfill.start(PHONE, "-1001", "-1002")
    await wait_for(lambda: client.sent == [1])
    await first.backfill.stop()

    second = make_service(tmp_path, lambda **_: FakeHistoryClient([]))
    await second.backfill.resume()

    assert second.backfill.task is None
    assert (await second.snapshot())["backfill"]["saved"]["source"] == "-1001"


@pytest.mark.asyncio
async def test_account_is_protected_while_its_history_is_copied(tmp_path):
    save_account(tmp_path)
    client = FakeFloodAtClient([photo(1), photo(2)])
    service = make_service(tmp_path, lambda **_: client)

    await service.backfill.start(PHONE, "-1001", "-1002")
    await wait_for(lambda: client.sent == [1])
    with pytest.raises(ServiceError):
        await service.backfill.start(PHONE, "-1001", "-1002")
    with pytest.raises(ServiceError):
        await service.request_login_code(PHONE)
    with pytest.raises(ServiceError):
        await service.remove_account(PHONE)
    await service.backfill.stop()


@pytest.mark.asyncio
async def test_live_forwarding_still_leaves_video_files_alone(tmp_path):
    save_account(tmp_path)

    class LiveClient(FakeHistoryClient):
        """Receives one video file after the live job has started."""

        async def get_chat_history(self, chat_id, limit=0):
            if limit == 1:
                yield FakeMessage(10)
                return
            yield FakeMessage(11, document=FakeDocument("video/mp4", "clip.mp4"))
            yield FakeMessage(10)

    client = LiveClient([])
    service = make_service(tmp_path, lambda **_: client)

    await service.start_forwarding(PHONE, "-1001", "-1002")
    await wait_for(lambda: service.forward_job.get("last_id") == 11)
    await service.stop_forwarding()

    assert client.sent == []

import json
import asyncio
from pathlib import Path

import pytest
from pyrogram import errors

from webapp.telegram_service import (
    ServiceError,
    TelegramService,
    TelegramSettings,
    sender_line,
)


class FakeUser:
    first_name = "Owner"


class FakeCode:
    phone_code_hash = "code-hash"


class FakeClient:
    def __init__(self):
        self.calls = []

    async def connect(self):
        self.calls.append("connect")

    async def send_code(self, phone):
        self.calls.append(("send_code", phone))
        return FakeCode()

    async def sign_in(self, phone, code_hash, code):
        self.calls.append(("sign_in", phone, code_hash, code))

    async def get_me(self):
        return FakeUser()

    async def disconnect(self):
        self.calls.append("disconnect")


class FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id


class FakePhotoMessage:
    id = 11
    photo = True
    video = None
    animation = None
    video_note = None
    caption = "New photo"
    caption_entities = None

    async def download(self, file_name):
        path = Path(file_name) / "photo.jpg"
        path.write_bytes(b"photo")
        return str(path)


class FakeForwardClient:
    def __init__(self):
        self.history_calls = 0
        self.sent = []

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def get_chat(self, chat_id):
        return FakeChat(chat_id)

    async def get_chat_history(self, chat_id, limit=0):
        self.history_calls += 1
        if self.history_calls == 1:
            marker = type("Message", (), {"id": 10})()
            yield marker
        elif self.history_calls == 2:
            yield FakePhotoMessage()
            marker = type("Message", (), {"id": 10})()
            yield marker

    async def send_photo(self, **kwargs):
        self.sent.append(kwargs)


class FakeFloodWaitClient(FakeForwardClient):
    async def send_photo(self, **kwargs):
        self.sent.append(kwargs)
        raise errors.FloodWait(3600)


class FakeRetryClient(FakeForwardClient):
    def __init__(self):
        super().__init__()
        self.send_attempts = 0

    async def get_chat_history(self, chat_id, limit=0):
        self.history_calls += 1
        if self.history_calls == 1:
            yield type("Message", (), {"id": 10})()
        else:
            yield FakePhotoMessage()
            yield type("Message", (), {"id": 10})()

    async def send_photo(self, **kwargs):
        self.send_attempts += 1
        if self.send_attempts == 1:
            raise OSError("temporary upload error")
        await super().send_photo(**kwargs)


def save_account(tmp_path, phone):
    account_dir = tmp_path / "account"
    config_dir = tmp_path / "data"
    account_dir.mkdir(exist_ok=True)
    config_dir.mkdir(exist_ok=True)
    (account_dir / f"{phone}.session").touch()
    (config_dir / f"{phone}.json").write_text(json.dumps({"phone": phone}))


@pytest.mark.asyncio
async def test_login_creates_account_marker_without_storing_password(tmp_path):
    client = FakeClient()
    settings = TelegramSettings(
        data_dir=tmp_path,
        api_id=12345,
        api_hash="api-hash",
        proxies=(),
        poll_seconds=0.01,
    )
    service = TelegramService(settings, client_factory=lambda **_: client)

    requested = await service.request_login_code("+15551234567")
    signed_in = await service.verify_login_code("+15551234567", "12345")

    marker = json.loads((tmp_path / "data" / "+15551234567.json").read_text())
    assert requested["status"] == "code_required"
    assert signed_in["status"] == "signed_in"
    assert marker == {"phone": "+15551234567"}
    assert "disconnect" not in client.calls
    assert service.clients["+15551234567"] is client

    await service.shutdown()
    assert client.calls[-1] == "disconnect"


@pytest.mark.asyncio
async def test_forwarding_worker_reuploads_new_media_and_cleans_download(tmp_path):
    phone = "+15551234567"
    save_account(tmp_path, phone)

    client = FakeForwardClient()
    settings = TelegramSettings(
        data_dir=tmp_path,
        api_id=12345,
        api_hash="api-hash",
        proxies=(),
        poll_seconds=0.01,
    )
    service = TelegramService(settings, client_factory=lambda **_: client)

    await service.start_forwarding(phone, "-1001", "-1002")
    for _ in range(100):
        if client.sent:
            break
        await asyncio.sleep(0.01)
    await service.stop_forwarding()

    state = await service.snapshot()
    assert client.sent[0]["chat_id"] == -1002
    assert state["forwarding"]["success"] == 1
    assert state["forwarding"]["status"] == "stopped"
    assert list((tmp_path / "downloads").iterdir()) == []


@pytest.mark.asyncio
async def test_stop_interrupts_telegram_rate_limit_wait(tmp_path):
    phone = "+15551234567"
    save_account(tmp_path, phone)

    client = FakeFloodWaitClient()
    settings = TelegramSettings(
        data_dir=tmp_path,
        api_id=12345,
        api_hash="api-hash",
        proxies=(),
        poll_seconds=0.01,
    )
    service = TelegramService(settings, client_factory=lambda **_: client)

    await service.start_forwarding(phone, "-1001", "-1002")
    for _ in range(100):
        state = await service.snapshot()
        if state["forwarding"]["failed"]:
            break
        await asyncio.sleep(0.01)
    await asyncio.wait_for(service.stop_forwarding(), timeout=0.5)

    state = await service.snapshot()
    assert state["forwarding"]["failed"] == 1
    assert state["forwarding"]["status"] == "stopped"


@pytest.mark.asyncio
async def test_forwarding_retries_failed_message_before_advancing(tmp_path):
    phone = "+15551234567"
    save_account(tmp_path, phone)

    client = FakeRetryClient()
    settings = TelegramSettings(
        data_dir=tmp_path,
        api_id=12345,
        api_hash="api-hash",
        proxies=(),
        poll_seconds=0.01,
    )
    service = TelegramService(settings, client_factory=lambda **_: client)

    await service.start_forwarding(phone, "-1001", "-1002")
    for _ in range(100):
        state = await service.snapshot()
        if state["forwarding"]["success"]:
            break
        await asyncio.sleep(0.01)
    await service.stop_forwarding()

    state = await service.snapshot()
    assert client.send_attempts == 2
    assert state["forwarding"]["failed"] == 1
    assert state["forwarding"]["success"] == 1
    assert state["forwarding"]["last_forwarded"] == 11


class FakeSender:
    def __init__(self, first_name, last_name=None, username=None):
        self.first_name = first_name
        self.last_name = last_name
        self.username = username


class FakeTitledChat:
    def __init__(self, title, username=None):
        self.id = -1009
        self.title = title
        self.username = username


class FakeOrigin:
    def __init__(self, **fields):
        self.__dict__.update(fields)


class FakeSentMessage:
    id = 77


class FakeVideoNote:
    duration = 3
    length = 240


class FakeUserPhotoMessage(FakePhotoMessage):
    from_user = FakeSender("Somchai", "Jaidee", "somchai")


class FakeVideoNoteMessage(FakePhotoMessage):
    photo = None
    video_note = FakeVideoNote()
    caption = None
    from_user = FakeSender("Somchai")


class FakeSenderClient(FakeForwardClient):
    message_class = FakeUserPhotoMessage

    def __init__(self):
        super().__init__()
        self.texts = []

    async def get_chat_history(self, chat_id, limit=0):
        self.history_calls += 1
        if self.history_calls == 1:
            yield type("Message", (), {"id": 10})()
        elif self.history_calls == 2:
            yield self.message_class()
            yield type("Message", (), {"id": 10})()

    async def send_photo(self, **kwargs):
        self.sent.append(kwargs)
        return FakeSentMessage()

    async def send_video_note(self, **kwargs):
        self.sent.append(kwargs)
        return FakeSentMessage()

    async def send_message(self, **kwargs):
        self.texts.append(kwargs)


class FakeDialogClient(FakeForwardClient):
    def __init__(self):
        super().__init__()
        self.connects = 0

    async def connect(self):
        self.connects += 1

    async def get_dialogs(self):
        return
        yield


class FakeRevokedClient(FakeForwardClient):
    def __init__(self):
        super().__init__()
        self.disconnected = False

    async def disconnect(self):
        self.disconnected = True

    async def get_chat_history(self, chat_id, limit=0):
        self.history_calls += 1
        if self.history_calls == 1:
            yield type("Message", (), {"id": 10})()
        else:
            raise errors.AuthKeyUnregistered()


class FakeFlakyClient(FakeForwardClient):
    """Loses its connection once, after the job has started."""

    dropped = False

    async def get_chat_history(self, chat_id, limit=0):
        self.history_calls += 1
        if self.history_calls == 1:
            yield type("Message", (), {"id": 10})()
        elif not FakeFlakyClient.dropped:
            FakeFlakyClient.dropped = True
            raise ConnectionResetError("connection lost")
        else:
            yield FakePhotoMessage()
            yield type("Message", (), {"id": 10})()


def make_service(tmp_path, client_factory, proxies=()):
    settings = TelegramSettings(
        data_dir=tmp_path,
        api_id=12345,
        api_hash="api-hash",
        proxies=proxies,
        poll_seconds=0.01,
    )
    return TelegramService(settings, client_factory=client_factory)


async def wait_for(condition):
    for _ in range(200):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not reached")


def test_sender_line_names_the_poster_and_the_forward_origin():
    message = type("Message", (), {})()
    assert sender_line(message) is None

    message.from_user = FakeSender("Somchai", "Jaidee", "somchai")
    assert sender_line(message) == "👤 Somchai Jaidee (@somchai)"

    message.forward_origin = FakeOrigin(chat=FakeTitledChat("News ABC"))
    assert sender_line(message) == "👤 Somchai Jaidee (@somchai) · ↪ News ABC"

    message.forward_origin = FakeOrigin(sender_user_name="Hidden Person")
    assert sender_line(message) == "👤 Somchai Jaidee (@somchai) · ↪ Hidden Person"

    channel_post = type("Message", (), {})()
    channel_post.sender_chat = FakeTitledChat("Source channel")
    channel_post.author_signature = "Editor"
    channel_post.forward_origin = FakeOrigin(sender_user=FakeSender("Mali"))
    assert sender_line(channel_post) == "👤 Source channel (Editor) · ↪ Mali"


@pytest.mark.asyncio
async def test_forwarded_caption_ends_with_the_sender_name(tmp_path):
    phone = "+15551234567"
    save_account(tmp_path, phone)
    client = FakeSenderClient()
    service = make_service(tmp_path, lambda **_: client)

    await service.start_forwarding(phone, "-1001", "-1002")
    await wait_for(lambda: client.sent)
    await service.stop_forwarding()

    assert client.sent[0]["caption"] == "New photo\n\n👤 Somchai Jaidee (@somchai)"
    assert client.texts == []


@pytest.mark.asyncio
async def test_sender_name_follows_as_a_reply_when_it_cannot_be_a_caption(tmp_path):
    phone = "+15551234567"
    save_account(tmp_path, phone)

    class LongCaptionMessage(FakeUserPhotoMessage):
        caption = "x" * 1020

    for message_class in (FakeVideoNoteMessage, LongCaptionMessage):
        client = FakeSenderClient()
        client.message_class = message_class
        service = make_service(tmp_path, lambda **_: client)

        await service.start_forwarding(phone, "-1001", "-1002")
        await wait_for(lambda: client.texts)
        await service.stop_forwarding()

        assert client.sent[0].get("caption") == message_class.caption
        assert client.texts[0]["chat_id"] == -1002
        assert client.texts[0]["text"].startswith("👤 Somchai")
        assert client.texts[0]["reply_parameters"].message_id == 77


@pytest.mark.asyncio
async def test_account_uses_one_client_with_a_stable_identity(tmp_path):
    phone = "+15551234567"
    save_account(tmp_path, phone)
    client = FakeDialogClient()
    created = []

    def factory(**kwargs):
        created.append(kwargs)
        return client

    proxies = ("a.example:1080:u:p", "b.example:1080:u:p", "c.example:1080:u:p")
    service = make_service(tmp_path, factory, proxies=proxies)

    await service.list_chats(phone)
    await service.start_forwarding(phone, "-1001", "-1002")
    await service.list_chats(phone)
    await service.stop_forwarding()

    assert len(created) == 1
    assert client.connects == 1
    assert created[0]["device_model"] == "Telegram Forwarder"
    assert created[0]["system_version"] == "Linux"
    assert created[0]["app_version"] == "1.0"
    assert {service._proxy(phone)["hostname"] for _ in range(20)} == {
        created[0]["proxy"]["hostname"]
    }


@pytest.mark.asyncio
async def test_revoked_session_asks_for_a_new_login(tmp_path):
    phone = "+15551234567"
    save_account(tmp_path, phone)
    client = FakeRevokedClient()
    service = make_service(tmp_path, lambda **_: client)

    await service.start_forwarding(phone, "-1001", "-1002")
    await wait_for(lambda: not service.forward_state["active"])

    state = await service.snapshot()
    assert state["forwarding"]["status"] == "reauth_required"
    assert client.disconnected
    assert phone not in service.clients


@pytest.mark.asyncio
async def test_lost_connection_reconnects_and_keeps_forwarding(tmp_path):
    phone = "+15551234567"
    save_account(tmp_path, phone)
    FakeFlakyClient.dropped = False
    created = []

    def factory(**_):
        created.append(FakeFlakyClient())
        return created[-1]

    service = make_service(tmp_path, factory)

    await service.start_forwarding(phone, "-1001", "-1002")
    await wait_for(lambda: service.forward_state["success"])
    await service.stop_forwarding()

    assert len(created) == 2
    assert created[1].sent[0]["chat_id"] == -1002
    assert service.forward_state["status"] == "stopped"


@pytest.mark.asyncio
async def test_login_is_refused_for_the_account_that_is_forwarding(tmp_path):
    phone = "+15551234567"
    save_account(tmp_path, phone)
    service = make_service(tmp_path, lambda **_: FakeForwardClient())

    await service.start_forwarding(phone, "-1001", "-1002")
    with pytest.raises(ServiceError):
        await service.request_login_code(phone)
    await service.stop_forwarding()


@pytest.mark.asyncio
async def test_removing_an_account_logs_out_of_telegram(tmp_path):
    phone = "+15551234567"
    save_account(tmp_path, phone)

    class FakeLogoutClient(FakeClient):
        async def invoke(self, query):
            self.calls.append(type(query).__name__)

    client = FakeLogoutClient()
    service = make_service(tmp_path, lambda **_: client)

    await service.remove_account(phone)

    assert client.calls == ["connect", "LogOut", "disconnect"]
    assert not (tmp_path / "account" / f"{phone}.session").exists()
    assert (await service.snapshot())["accounts"] == []


class FakeBacklogClient(FakeForwardClient):
    """Holds one message that arrived while the service was down."""

    async def get_chat_history(self, chat_id, limit=0):
        self.history_calls += 1
        if self.history_calls == 1:
            yield FakePhotoMessage()
            yield type("Message", (), {"id": 10})()
        else:
            yield type("Message", (), {"id": 11})()


@pytest.mark.asyncio
async def test_forwarding_resumes_after_a_restart_where_it_left_off(tmp_path):
    phone = "+15551234567"
    save_account(tmp_path, phone)
    first = make_service(tmp_path, lambda **_: FakeForwardClient())

    await first.start_forwarding(phone, "-1001", "-1002")
    await wait_for(lambda: first.forward_state["status"] == "running")
    first.job_file.write_text(
        json.dumps(
            {"phone": phone, "source": "-1001", "destination": "-1002", "last_id": 10}
        )
    )
    await first.shutdown()
    assert first.job_file.exists()

    client = FakeBacklogClient()
    second = make_service(tmp_path, lambda **_: client)
    await second.resume_forwarding()
    await wait_for(lambda: client.sent)

    assert client.sent[0]["chat_id"] == -1002
    assert json.loads(second.job_file.read_text())["last_id"] == 11
    await second.shutdown()


@pytest.mark.asyncio
async def test_stopped_job_is_not_resumed(tmp_path):
    phone = "+15551234567"
    save_account(tmp_path, phone)
    first = make_service(tmp_path, lambda **_: FakeForwardClient())

    await first.start_forwarding(phone, "-1001", "-1002")
    await first.stop_forwarding()

    second = make_service(tmp_path, lambda **_: FakeForwardClient())
    await second.resume_forwarding()

    assert not second.job_file.exists()
    assert second.forward_task is None


@pytest.mark.asyncio
async def test_revoked_session_does_not_resume(tmp_path):
    phone = "+15551234567"
    save_account(tmp_path, phone)
    service = make_service(tmp_path, lambda **_: FakeRevokedClient())

    await service.start_forwarding(phone, "-1001", "-1002")
    await wait_for(lambda: not service.forward_state["active"])

    assert not service.job_file.exists()

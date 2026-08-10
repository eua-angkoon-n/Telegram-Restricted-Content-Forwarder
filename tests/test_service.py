import json
import asyncio
from pathlib import Path

import pytest
from pyrogram import errors

from webapp.telegram_service import TelegramService, TelegramSettings


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

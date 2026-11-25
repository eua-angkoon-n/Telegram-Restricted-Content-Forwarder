from fastapi.testclient import TestClient

from webapp.main import create_app


class FakeBackfill:
    def __init__(self):
        self.requests = []

    async def resume(self):
        self.requests.append(("resume",))

    async def start(self, phone, source, destination, restart=False):
        self.requests.append(("start", phone, source, destination, restart))
        return {"status": "starting"}

    async def stop(self):
        self.requests.append(("stop",))
        return {"status": "stopped"}


class FakeTelegramService:
    def __init__(self):
        self.login_requests = []
        self.forward_requests = []
        self.backfill = FakeBackfill()

    async def snapshot(self):
        return {
            "accounts": [],
            "forwarding": {
                "active": False,
                "status": "stopped",
                "success": 0,
                "failed": 0,
                "total": 0,
                "last_forwarded": None,
                "logs": [],
            },
        }

    async def request_login_code(self, phone):
        self.login_requests.append(("request", phone))
        return {"status": "code_required", "phone": phone}

    async def verify_login_code(self, phone, code):
        self.login_requests.append(("code", phone, code))
        return {"status": "signed_in", "phone": phone}

    async def verify_login_password(self, phone, password):
        self.login_requests.append(("password", phone, password))
        return {"status": "signed_in", "phone": phone}

    async def list_chats(self, phone):
        return [{"id": -100123, "title": "Source channel"}]

    async def remove_account(self, phone):
        self.login_requests.append(("remove", phone))

    async def start_forwarding(self, phone, source, destination):
        self.forward_requests.append(("start", phone, source, destination))
        return {"status": "starting"}

    async def stop_forwarding(self):
        self.forward_requests.append(("stop",))
        return {"status": "stopping"}


def test_health_is_public_and_api_requires_auth():
    app = create_app(FakeTelegramService(), username="owner", password="secret")

    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        assert client.get("/api/state").status_code == 401

        response = client.get("/api/state", auth=("owner", "secret"))

    assert response.status_code == 200
    assert response.json()["forwarding"]["status"] == "stopped"


def test_owner_can_complete_account_code_login():
    service = FakeTelegramService()
    app = create_app(service, username="owner", password="secret")

    with TestClient(app) as client:
        auth = ("owner", "secret")
        requested = client.post(
            "/api/accounts/request-code",
            auth=auth,
            json={"phone": "+15551234567"},
        )
        verified = client.post(
            "/api/accounts/verify-code",
            auth=auth,
            json={"phone": "+15551234567", "code": "12345"},
        )

    assert requested.json()["status"] == "code_required"
    assert verified.json()["status"] == "signed_in"
    assert service.login_requests == [
        ("request", "+15551234567"),
        ("code", "+15551234567", "12345"),
    ]


def test_owner_can_finish_2fa_and_manage_account():
    service = FakeTelegramService()
    app = create_app(service, username="owner", password="secret")

    with TestClient(app) as client:
        auth = ("owner", "secret")
        password_result = client.post(
            "/api/accounts/verify-password",
            auth=auth,
            json={"phone": "+15551234567", "password": "not-persisted"},
        )
        chats = client.get("/api/accounts/%2B15551234567/chats", auth=auth)
        removed = client.delete("/api/accounts/%2B15551234567", auth=auth)

    assert password_result.json()["status"] == "signed_in"
    assert chats.json() == [{"id": -100123, "title": "Source channel"}]
    assert removed.status_code == 204
    assert service.login_requests == [
        ("password", "+15551234567", "not-persisted"),
        ("remove", "+15551234567"),
    ]


def test_owner_can_start_and_stop_forwarding():
    service = FakeTelegramService()
    app = create_app(service, username="owner", password="secret")

    with TestClient(app) as client:
        auth = ("owner", "secret")
        started = client.post(
            "/api/forward/start",
            auth=auth,
            json={
                "phone": "+15551234567",
                "source": "@source_channel",
                "destination": "-1009876543210",
            },
        )
        stopped = client.post("/api/forward/stop", auth=auth, json={})

    assert started.status_code == 202
    assert stopped.status_code == 202
    assert service.forward_requests == [
        ("start", "+15551234567", "@source_channel", "-1009876543210"),
        ("stop",),
    ]


def test_stop_rejects_cross_site_form_content_type():
    service = FakeTelegramService()
    app = create_app(service, username="owner", password="secret")

    with TestClient(app) as client:
        response = client.post(
            "/api/forward/stop",
            auth=("owner", "secret"),
            data={},
        )

    assert response.status_code == 415
    assert service.forward_requests == []


def test_history_page_requires_auth():
    app = create_app(FakeTelegramService(), username="owner", password="secret")

    with TestClient(app) as client:
        assert client.get("/history").status_code == 401

        response = client.get("/history", auth=("owner", "secret"))

    assert response.status_code == 200
    assert "Copy history" in response.text


def test_owner_can_start_and_stop_a_history_job():
    service = FakeTelegramService()
    app = create_app(service, username="owner", password="secret")

    with TestClient(app) as client:
        auth = ("owner", "secret")
        route = {
            "phone": "+15551234567",
            "source": "@source_channel",
            "destination": "-1009876543210",
        }
        started = client.post("/api/backfill/start", auth=auth, json=route)
        restarted = client.post(
            "/api/backfill/start", auth=auth, json={**route, "restart": True}
        )
        rejected = client.post("/api/backfill/stop", auth=auth, data={})
        stopped = client.post("/api/backfill/stop", auth=auth, json={})

    assert started.status_code == 202
    assert restarted.status_code == 202
    assert rejected.status_code == 415
    assert stopped.status_code == 202
    assert service.backfill.requests == [
        ("resume",),
        ("start", "+15551234567", "@source_channel", "-1009876543210", False),
        ("start", "+15551234567", "@source_channel", "-1009876543210", True),
        ("stop",),
    ]


def test_api_documentation_is_not_publicly_exposed():
    app = create_app(FakeTelegramService(), username="owner", password="secret")

    with TestClient(app) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/redoc").status_code == 404
        assert client.get("/openapi.json").status_code == 404

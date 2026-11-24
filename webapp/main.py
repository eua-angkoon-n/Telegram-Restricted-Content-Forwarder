from contextlib import asynccontextmanager
import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from webapp.telegram_service import ServiceError


class PhoneRequest(BaseModel):
    phone: str = Field(pattern=r"^\+[1-9]\d{3,14}$")


class CodeRequest(PhoneRequest):
    code: str = Field(pattern=r"^\d{5,6}$")


class PasswordRequest(PhoneRequest):
    password: str = Field(min_length=1, max_length=256)


class ForwardRequest(PhoneRequest):
    source: str = Field(min_length=1, max_length=256)
    destination: str = Field(min_length=1, max_length=256)


def create_app(service, *, username: str, password: str) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app):
        resume = getattr(service, "resume_forwarding", None)
        if resume:
            await resume()
        yield
        shutdown = getattr(service, "shutdown", None)
        if shutdown:
            await shutdown()

    app = FastAPI(
        title="Telegram Forwarder",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    security = HTTPBasic(auto_error=False)
    static_dir = Path(__file__).parent / "static"

    async def require_auth(
        credentials: HTTPBasicCredentials | None = Depends(security),
    ) -> None:
        valid = (
            credentials is not None
            and secrets.compare_digest(credentials.username, username)
            and secrets.compare_digest(credentials.password, password)
        )
        if not valid:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
                headers={"WWW-Authenticate": "Basic"},
            )

    async def require_json(request: Request) -> None:
        media_type = request.headers.get("content-type", "").partition(";")[0]
        if media_type.strip().lower() != "application/json":
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail="Content-Type must be application/json",
            )

    @app.exception_handler(ServiceError)
    async def service_error_handler(_request, exc: ServiceError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/", dependencies=[Depends(require_auth)], include_in_schema=False)
    async def index():
        return FileResponse(static_dir / "index.html")

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/api/state", dependencies=[Depends(require_auth)])
    async def get_state():
        return await service.snapshot()

    @app.post("/api/accounts/request-code", dependencies=[Depends(require_auth)])
    async def request_code(payload: PhoneRequest):
        return await service.request_login_code(payload.phone)

    @app.post("/api/accounts/verify-code", dependencies=[Depends(require_auth)])
    async def verify_code(payload: CodeRequest):
        return await service.verify_login_code(payload.phone, payload.code)

    @app.post("/api/accounts/verify-password", dependencies=[Depends(require_auth)])
    async def verify_password(payload: PasswordRequest):
        return await service.verify_login_password(payload.phone, payload.password)

    @app.get("/api/accounts/{phone}/chats", dependencies=[Depends(require_auth)])
    async def list_chats(phone: str):
        return await service.list_chats(phone)

    @app.delete(
        "/api/accounts/{phone}",
        dependencies=[Depends(require_auth)],
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def remove_account(phone: str):
        await service.remove_account(phone)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post(
        "/api/forward/start",
        dependencies=[Depends(require_auth)],
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def start_forwarding(payload: ForwardRequest):
        return await service.start_forwarding(
            payload.phone,
            payload.source.strip(),
            payload.destination.strip(),
        )

    @app.post(
        "/api/forward/stop",
        dependencies=[Depends(require_auth), Depends(require_json)],
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def stop_forwarding():
        return await service.stop_forwarding()

    return app

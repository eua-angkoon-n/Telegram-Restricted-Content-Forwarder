from webapp.config import AppSettings
from webapp.main import create_app
from webapp.telegram_service import TelegramService


settings = AppSettings.from_env()
service = TelegramService(settings.telegram)
app = create_app(service, username=settings.username, password=settings.password)

# Telegram Restricted Content Forwarder

A private, single-owner Web UI for re-uploading newly received photo and video media from a Telegram source channel to a destination channel. The application is designed to run as one persistent Northflank service.

Use only with channels and content you are authorized to access and process. Follow Telegram's terms and applicable law.

## Run locally with Docker

Create Telegram API credentials at `my.telegram.org`, then run:

```bash
docker build -t telegram-forwarder .
docker run --rm -p 8080:8080 -v telegram-forwarder-data:/data \
  -e APP_USERNAME=owner \
  -e APP_PASSWORD='replace-with-a-long-random-password' \
  -e TELEGRAM_API_ID=123456 \
  -e TELEGRAM_API_HASH='replace-with-your-api-hash' \
  telegram-forwarder
```

Open `http://localhost:8080` and enter the Basic Auth credentials when prompted.

Optional environment variables:

- `APP_DATA_DIR` — persistent state path; default `/data`
- `POLL_SECONDS` — source polling interval; default `5`
- `TELEGRAM_PROXIES` — SOCKS5 proxies as `host:port:username:password`, separated by semicolons or new lines

## Deploy on Northflank

1. Create a combined service from this repository and select **Dockerfile** build type.
2. Add runtime secrets from `.env.example`: `APP_USERNAME`, `APP_PASSWORD`, `TELEGRAM_API_ID`, and `TELEGRAM_API_HASH`.
3. Add public HTTP port `8080`.
4. Add an HTTP health check for `/healthz` on port `8080`.
5. Add a **Single Read/Write** persistent volume mounted at `/data`.
6. Keep the service at **one instance**. Telegram session files must not be opened by multiple replicas.
7. Deploy and complete Telegram OTP/2FA sign-in from the Web UI.

The two-step verification password is used only for the sign-in request and is not stored. Telegram session files and account markers are stored on the `/data` volume.

## Development

```bash
python -m pip install -r requirements-dev.txt
pytest
```

The test suite covers Web authentication/API workflows, account login persistence, and the forwarding worker lifecycle.

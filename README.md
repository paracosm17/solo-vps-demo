# Hello application

A small application for exercising GitHub Actions → GHCR → Coolify.

Run locally with `python app.py`; open `http://localhost:8080`.
Run tests with `python -m unittest discover -s . -p 'test_*.py'`.
Install `requirements-dev.txt` in a local virtual environment to run Ruff lint and format checks.

The first push to `main` runs tests, lint/format checks, builds an image, publishes it to GHCR and verifies its digest. Deployment remains disabled until the **repository variable** `SOLO_VPS_DEPLOY_ENABLED` is `true`.

Follow Solo VPS's **First application** guide for the initial image, registry access, Coolify Docker Image resource, domain/TLS and production environment settings. Once the first immutable image is healthy and credentials are configured, enable CD and push the next change.

Set the non-secret runtime variable `APP_MESSAGE` in Coolify and redeploy to see it at `/`. Change `APP_VERSION` in `app.py` to demonstrate a new release. `/healthz` is the health endpoint. This teaching app has no database; adapt tests, lint commands, health checks and the migration hook for your real application.

Never commit tokens, deployment keys or production `.env` files. Failed container releases can roll back the previous image; this does not undo database migrations or application data. A deployment with unknown status stops for inspection instead of starting an overlapping rollback.

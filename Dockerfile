FROM python:3.12-slim

RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# F-20: create the unprivileged runtime user before COPY-ing app sources so we
# can use --chown and avoid leaving the app directory root-owned + world
# readable inside the image.
RUN adduser --disabled-password --gecos '' appuser

# Dependency install must run as root to write into the system site-packages.
COPY pyproject.toml .
RUN pip install --no-cache-dir .

COPY --chown=appuser:appuser app/ app/

EXPOSE 8080

USER appuser

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]

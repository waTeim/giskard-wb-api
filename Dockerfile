# syntax=docker/dockerfile:1

FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first, in their own layer, so source changes rebuild quickly.
COPY pyproject.toml ./
RUN mkdir -p src/giskard_wb_api && touch src/giskard_wb_api/__init__.py \
 && pip install . && pip uninstall -y giskard-wb-api && rm -rf src build

COPY src ./src
RUN pip install --no-deps . && rm -rf src build

# uid 1000 matches the chart's podSecurityContext. HOME holds the SkyPilot
# client's state (~/.sky); the chart mounts a writable volume there.
RUN useradd --uid 1000 --user-group --create-home --home-dir /home/app app \
 && mkdir -p /data /etc/giskard-wb-api && chown app:app /data
USER app

# All settings come from the config file, by default
# /etc/giskard-wb-api/config.yaml: mount it there (the Helm chart does), or
# pass another path with --config. See config.example.yaml.
VOLUME ["/data"]
EXPOSE 8080

# Assumes the default server.port.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"

ENTRYPOINT ["giskard-wb-api"]

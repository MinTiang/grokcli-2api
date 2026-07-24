# Standalone Grok account registration service
# Protocol register (mail + captcha + device flow) + Web UI + push to grokcli-2api
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai \
    DEBIAN_FRONTEND=noninteractive \
    HOME=/root \
    PYTHONPATH=/app:/app/grok-build-auth \
    STANDALONE_VENDOR_ROOT=/app \
    GROK2API_DATA_DIR=/app/data \
    GROK2API_STORE_BACKEND=file \
    GROK2API_REG_SKIP_PROBE=1 \
    GROK2API_CAPTCHA_PROVIDER=local \
    CAPTCHA_PROVIDER=local \
    GROK2API_LOCAL_SOLVER_URL=http://127.0.0.1:5072 \
    LOCAL_SOLVER_URL=http://127.0.0.1:5072 \
    GROK2API_INLINE_SOLVER=1 \
    TURNSTILE_HOST=127.0.0.1 \
    TURNSTILE_PORT=5072 \
    TURNSTILE_THREAD=3 \
    TURNSTILE_BROWSER_TYPE=camoufox \
    TURNSTILE_LAZY=1 \
    TURNSTILE_IDLE_SEC=180 \
    REG_UI_HOST=0.0.0.0 \
    REG_UI_PORT=8080

WORKDIR /app

# Browser runtime libs for inline Turnstile Solver (Camoufox/Firefox)
# Use Aliyun Debian mirror for faster apt in CN
RUN set -eux; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
      sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources; \
    fi; \
    if [ -f /etc/apt/sources.list ]; then \
      sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list; \
    fi; \
    apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        fonts-liberation \
        fonts-noto-color-emoji \
        libasound2 \
        libatk-bridge2.0-0 \
        libatk1.0-0 \
        libcups2 \
        libdbus-1-3 \
        libdrm2 \
        libgbm1 \
        libgtk-3-0 \
        libnspr4 \
        libnss3 \
        libpango-1.0-0 \
        libx11-6 \
        libx11-xcb1 \
        libxcb1 \
        libxcomposite1 \
        libxdamage1 \
        libxext6 \
        libxfixes3 \
        libxkbcommon0 \
        libxrandr2 \
        libxshmfence1 \
        libxss1 \
        libxtst6 \
        tzdata \
        xvfb \
    && ln -snf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
    && echo Asia/Shanghai > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# Python deps (service + solver)
COPY standalone-registration/requirements.txt /app/requirements-standalone.txt
COPY requirements.txt /app/requirements-core.txt
COPY turnstile-solver/requirements.txt /app/requirements-solver.txt
RUN python -m pip install --no-cache-dir -U pip setuptools wheel \
    && python -m pip install --no-cache-dir -r /app/requirements-core.txt \
    && python -m pip install --no-cache-dir -r /app/requirements-standalone.txt \
    && python -m pip install --no-cache-dir -r /app/requirements-solver.txt \
    && python -m camoufox fetch \
    && python -m patchright install chromium || true

# Vendored registration stack from parent project
COPY grok2api /app/grok2api
COPY grok-build-auth /app/grok-build-auth
COPY turnstile-solver /app/turnstile-solver
COPY scripts /app/scripts

# Standalone app
COPY standalone-registration/app /app/app
COPY standalone-registration/static /app/static
COPY standalone-registration/entrypoint.sh /app/entrypoint.sh

RUN chmod +x /app/entrypoint.sh \
    && mkdir -p /app/data /app/turnstile-solver/logs /app/turnstile-solver/keys \
    && test -f /app/grok-build-auth/xconsole_client/client.py \
    && test -f /app/grok2api/upstream/grok_build_adapter.py \
    && test -f /app/turnstile-solver/api_solver.py \
    && test -f /app/app/main.py \
    && python -c "from grok2api.upstream import grok_build_adapter; from app import config_store; print('build-ok', grok_build_adapter.ADAPTER_BUILD)"

EXPOSE 8080
VOLUME ["/app/data"]

ENTRYPOINT ["/app/entrypoint.sh"]

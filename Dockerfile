FROM python:3.11-slim

LABEL org.opencontainers.image.authors="joker2still"
LABEL org.opencontainers.image.licenses="MIT"

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
    libxrandr2 libgbm1 libasound2 libpango-1.0-0 libcairo2 \
    libx11-6 libx11-xcb1 libxcb1 libxext6 libxshmfence1 \
    git curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md LICENSE ./
RUN pip install --no-cache-dir setuptools wheel

COPY *.py ./
COPY skills/ skills/
COPY static/ static/
COPY migrations/ migrations/
COPY prompts/ prompts/
COPY schemas/ schemas/

RUN pip install --no-cache-dir -e . && \
    pip install --no-cache-dir playwright && \
    python -m playwright install chromium

ENV CASTOR_DATA_DIR=/data \
    CASTOR_QDRANT_MODE=disk \
    PYTHONUNBUFFERED=1
RUN mkdir -p /data && ln -sfn /data /root/.castor
VOLUME ["/data"]

EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD curl -fsS http://localhost:7860/ || exit 1

CMD ["castor", "--web", "--host", "0.0.0.0", "--port", "7860"]

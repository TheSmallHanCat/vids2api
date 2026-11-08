FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/TheSmallHanCat/vids2api" \
    org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    DISPLAY=:99 \
    VIDS2API_VNC_PORT=8690

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        xvfb \
        x11vnc \
        novnc \
        websockify \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && patchright install --with-deps chromium

COPY docker-entrypoint.sh main.py ./
COPY LICENSE ./
COPY src ./src
COPY static ./static
COPY config/setting_example.toml ./config/setting_example.toml

RUN chmod +x docker-entrypoint.sh && mkdir -p /app/data /app/tmp

EXPOSE 8687 8690

ENTRYPOINT ["./docker-entrypoint.sh"]

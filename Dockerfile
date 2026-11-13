FROM python:3.11-slim

# 允许在构建时替换 Debian / PyPI 源，适配国内服务器网络。
ARG APT_MIRROR=http://deb.debian.org/debian
ARG APT_SECURITY_MIRROR=http://deb.debian.org/debian-security
ARG PIP_INDEX_URL=https://pypi.org/simple

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN set -eux; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i "s|http://deb.debian.org/debian-security|${APT_SECURITY_MIRROR}|g" /etc/apt/sources.list.d/debian.sources; \
        sed -i "s|http://deb.debian.org/debian|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources; \
    fi; \
    apt-get update; \
    apt-get install --no-install-recommends -y libatomic1; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir \
    --index-url "${PIP_INDEX_URL}" \
    --only-binary=:all: \
    -r requirements.txt

# Playwright 浏览器（headless chromium）
RUN playwright install chromium --with-deps

COPY . .

# 持久化目录（容器删除后数据不丢）
RUN mkdir -p /app/data /app/logs /app/reports

EXPOSE 8000

# 启动 HTTP 服务 + Scheduler + AgentTask
CMD ["python", "-m", "uvicorn", "app.web:app", "--host", "0.0.0.0", "--port", "8000"]

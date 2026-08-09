FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 中文字体(浏览器渲染 HTML 截图需要)
RUN apt-get update && apt-get install -y --no-install-recommends fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

# 浏览器渲染(按浏览器视角截图邮件正文)需要 Chromium
RUN playwright install --with-deps chromium

COPY app/ ./app/
# 推送脚本目录(真实 custom_sender.py 含密钥,已在 .dockerignore 排除;
# 容器内要用 CUSTOM_SENDER 的话,基于 .example 模板自行创建)
COPY sender/ ./sender/

ENV PYTHONPATH=/app

# Run as a non-root user; /app/data is where the SQLite database lives.
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app

USER appuser

VOLUME /app/data

# Fail the health check if the SQLite database is no longer readable.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import sqlite3; sqlite3.connect('/app/data/data.db').execute('SELECT 1')"

CMD ["python", "-u", "app/main.py"]

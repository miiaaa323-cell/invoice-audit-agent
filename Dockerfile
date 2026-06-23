FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/app/runtime \
    LLM_MODE=replay

WORKDIR /app
COPY requirements.lock ./
RUN python -m pip install --no-cache-dir -r requirements.lock
COPY . .
RUN python -m pip install --no-cache-dir --no-deps -e . \
    && groupadd --gid 10001 invoice \
    && useradd --uid 10001 --gid invoice --no-create-home invoice \
    && mkdir -p /app/runtime \
    && chown invoice:invoice /app/runtime \
    && chown -R invoice:invoice /app/fixtures/llm_cache

USER invoice
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM python:3.11-slim AS builder

WORKDIR /build
COPY pyproject.toml .
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.11-slim

RUN useradd --create-home appuser
WORKDIR /app

COPY --from=builder /install /usr/local
COPY src/ src/
COPY config/ config/

RUN mkdir -p logs && chown -R appuser:appuser /app
USER appuser

EXPOSE 9090 8050

HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:9090/health')"

CMD ["python", "-m", "src.main"]

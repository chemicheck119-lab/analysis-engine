FROM python:3.11.15-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CHEMIGUARD119_PROJECT_ROOT=/app \
    CHEMIGUARD119_ARTIFACT_DIR=/opt/chemicheck119/artifacts \
    CHEMIGUARD119_CONFIG_DIR=/opt/chemicheck119/config \
    CHEMIGUARD119_API_HOST=0.0.0.0 \
    CHEMIGUARD119_API_PORT=8000

WORKDIR /app

RUN addgroup --system --gid 10001 chemicheck \
    && adduser --system --uid 10001 --ingroup chemicheck --home /nonexistent chemicheck

COPY pyproject.toml README.md requirements-production.txt ./
COPY src ./src
RUN python -m pip install --requirement requirements-production.txt \
    && python -m pip install --no-deps .

COPY config /opt/chemicheck119/config
RUN chown -R chemicheck:chemicheck /opt/chemicheck119

USER chemicheck
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=3)"]

CMD ["chemiguard119-api"]

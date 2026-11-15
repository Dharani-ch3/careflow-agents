# syntax=docker/dockerfile:1

FROM python:3.12-slim AS build
WORKDIR /app
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip wheel --wheel-dir /wheels ".[all]"

FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 CAREFLOW_DATA_DIR=/app/data
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels \
    && useradd --create-home --uid 10001 careflow \
    && mkdir -p /app/data && chown careflow /app/data
USER careflow
# Hosts like Render pass the port in $PORT; locally it falls back to 8000.
ENV PORT=8000
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import os, urllib.request as u; u.urlopen('http://localhost:%s/healthz' % os.environ['PORT'])"
CMD ["sh", "-c", "uvicorn careflow.api:app --host 0.0.0.0 --port ${PORT}"]

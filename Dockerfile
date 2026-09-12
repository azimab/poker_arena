# Sandbox image for untrusted bot submissions. Built by `poker_arena.sandbox.build_image()`
# or `docker build -t poker-arena-sandbox .`; run with no network, no mounts, read-only.
FROM python:3.11-slim AS build
WORKDIR /src
COPY pyproject.toml README.md ./
COPY poker_arena ./poker_arena
RUN pip install --no-cache-dir --target /opt/arena .

FROM python:3.11-slim
COPY --from=build /opt/arena /opt/arena
ENV PYTHONPATH=/opt/arena \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp
USER 65534:65534
ENTRYPOINT ["python", "-m", "poker_arena._sandbox_worker"]

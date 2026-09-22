# python:3.12-slim, not alpine: duckdb ships prebuilt glibc wheels only,
# so musl (alpine) forces a from-source build that needs a C++ toolchain.
FROM python:3.12-slim

# tzdata: the app has no zoneinfo/TZ handling of its own (see config.py) —
# it relies on the system timezone via datetime.astimezone(), which is
# silently UTC on slim images without this package. curl: container
# healthcheck probe (see docker-compose.yml).
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata curl \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin appuser

WORKDIR /app
COPY . .

# duckdb pinned to the exact version that wrote existing data files —
# DuckDB's storage format has changed across releases, and an unpinned
# resolve on rebuild could silently upgrade the on-disk format out from
# under a database no older duckdb build can open. Bump deliberately,
# together with a tested upgrade path for the live file, not as a side
# effect of `docker compose up --build`.
RUN pip install --no-cache-dir 'duckdb==1.5.4' .

ENV GOETTA_FINANCE_HOME=/data
VOLUME /data

USER appuser
EXPOSE 8765

ENTRYPOINT ["goetta-finance"]
CMD ["daemon", "--host", "0.0.0.0", "--port", "8765"]

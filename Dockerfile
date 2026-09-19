# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

# git is not optional here: the manuscript *is* a git repository and
# publish.py shells out to it on every compile. A slim image without it
# builds fine and then fails at the first commit.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    SARGAM_DATA=/data/users \
    SARGAM_ACCOUNTS=/data/accounts.db \
    PORT=8080

WORKDIR /app

# Dependencies first: they change far less often than the source, so this
# layer stays cached across ordinary deploys.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps .

RUN useradd --system --create-home --uid 10001 app

COPY deploy/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

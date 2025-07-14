# syntax=docker.io/docker/dockerfile:1.7-labs
FROM python:3.13.5-bookworm
ARG ENV

ENV LIBRARY_PATH=/lib:/usr/lib

COPY --from=ghcr.io/astral-sh/uv:0.6.6 /uv /bin/uv

# Prepare install
WORKDIR /virtualenv
COPY pyproject.toml uv.lock ./

RUN echo "Environment: ${ENV}"
RUN --mount=type=ssh \
    if [ "$ENV" = "dev" ]; then \
    echo 'Installing DEV' && uv sync --extra dev --frozen --no-install-project; \
    else \
    echo 'Installing REG' && uv sync --frozen --no-install-project; \
    fi

WORKDIR /app
COPY --exclude=**/.* --exclude=*.lock --exclude=*.toml . /app

ENV VIRTUAL_ENV=/virtualenv/.venv
ENV PATH="/virtualenv/.venv/bin:$PATH"
ENV PYTHONHASHSEED=0
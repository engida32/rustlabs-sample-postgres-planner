FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 \
        python3-pytest \
        postgresql \
        postgresql-client \
        passwd \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 agent

ENV PATH="/usr/lib/postgresql/15/bin:${PATH}"

WORKDIR /app
COPY workspace/ /app/
RUN chown -R agent:agent /app

USER agent

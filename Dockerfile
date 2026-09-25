# One image runs every role: edge agent, traffic server/client, monitor.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends iproute2 iputils-ping tcpdump \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY sdwan ./sdwan
RUN pip install --no-cache-dir .

COPY config/topology.toml /etc/sdwan/topology.toml

ENV PYTHONUNBUFFERED=1 \
    SDWAN_CONFIG=/etc/sdwan/topology.toml \
    SDWAN_DB=/data/sdwan.db

ENTRYPOINT ["sdwan"]
CMD ["--help"]

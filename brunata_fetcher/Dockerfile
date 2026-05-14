ARG BUILD_FROM
FROM ${BUILD_FROM}

# Python runtime. The HA base is minimal; install python + pip and drop the
# apt lists in the same layer so they don't bloat the image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*

# Python deps only — the add-on talks to the Brunata portal over plain HTTP
# (login included), so there's no browser to install. requirements.txt is
# httpx + paho-mqtt + websockets; no Playwright, no Chromium.
COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --break-system-packages --no-cache-dir -r /tmp/requirements.txt \
    && rm -rf /tmp/requirements.txt

# Copy add-on scripts
COPY *.py /app/
COPY run.sh /run.sh
RUN chmod +x /run.sh

WORKDIR /app
CMD ["/run.sh"]

ARG BUILD_ARCH
FROM ghcr.io/home-assistant/${BUILD_ARCH}-base-debian:bookworm

# Python runtime. The HA base is minimal; install python + pip and drop the
# apt lists in the same layer so they don't bloat the image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*

# Python deps + the Playwright Chromium headless-shell variant. The shell
# is ~100 MB instead of ~300 MB for the full Chromium build and `--with-deps`
# pulls a smaller set of system libs (no X11 / GTK / audio). server.py always
# launches with headless=True, so the full Chromium build would just be dead
# weight in the image. Apt lists pulled in by `--with-deps` are cleaned in
# the same RUN.
COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --break-system-packages --no-cache-dir -r /tmp/requirements.txt \
    && playwright install chromium-headless-shell --with-deps \
    && rm -rf /var/lib/apt/lists/* /tmp/requirements.txt

# Copy add-on scripts
COPY *.py /app/
COPY run.sh /run.sh
RUN chmod +x /run.sh

WORKDIR /app
CMD ["/run.sh"]

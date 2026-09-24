FROM python:3.14-slim@sha256:caaf356f40667c496d405780745b9ac25771c189a51dfcc42430d531ea09f8a2

# Standard library only: nothing is installed beyond the source itself.
WORKDIR /app
COPY src/simkl_bridge /app/simkl_bridge

# Unprivileged, with state (access token, id cache) in a volume it owns.
# pip is removed: the bridge needs only the standard library, and pip's
# vendored packages are the image's only known vulnerabilities.
RUN useradd --system --uid 10001 --no-create-home bridge \
 && mkdir /data && chown bridge /data && chmod 0700 /data \
 && rm -rf /usr/local/lib/python3.*/site-packages/pip* /usr/local/lib/python3.*/ensurepip \
           /usr/local/bin/pip*
USER 10001
VOLUME /data

ENV PYTHONUNBUFFERED=1 BRIDGE_DATA_DIR=/data BRIDGE_PORT=8080
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=10s \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"]

ENTRYPOINT ["python", "-m", "simkl_bridge"]
CMD ["serve"]

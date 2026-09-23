FROM python:3.13-slim@sha256:8d9d0b8bcf6506481eae4907c18f5e3e7902e629f5f6d684f9e7c32e85e3ddf0

# Standard library only: nothing is installed beyond the source itself.
WORKDIR /app
COPY src/simkl_bridge /app/simkl_bridge

# Unprivileged, with state (access token, id cache) in a volume it owns.
RUN useradd --system --uid 10001 --no-create-home bridge \
 && mkdir /data && chown bridge /data && chmod 0700 /data
USER 10001
VOLUME /data

ENV PYTHONUNBUFFERED=1 BRIDGE_DATA_DIR=/data BRIDGE_PORT=8080
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=10s \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"]

ENTRYPOINT ["python", "-m", "simkl_bridge"]
CMD ["serve"]

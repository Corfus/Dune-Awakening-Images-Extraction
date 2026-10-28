# Tool container only; no server, Docker daemon, or credentials baked in.
FROM ubuntu:24.04
COPY scripts/install-tools.sh /tmp/install-tools.sh
RUN sh /tmp/install-tools.sh \
 && rm /tmp/install-tools.sh && apt-get clean && rm -rf /var/lib/apt/lists/*
WORKDIR /workspace
COPY dune_mirror.py images.lock /opt/dune-mirror/
ENTRYPOINT ["python3", "/opt/dune-mirror/dune_mirror.py"]
CMD ["--help"]

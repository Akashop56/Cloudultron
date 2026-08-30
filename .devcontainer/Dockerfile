FROM mcr.microsoft.com/devcontainers/base:ubuntu

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && (apt-get install -y --no-install-recommends adb || apt-get install -y --no-install-recommends android-tools-adb) && apt-get install -y --no-install-recommends python3 python3-venv && (apt-get install -y --no-install-recommends docker.io || echo "no docker client") && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN printf '%s\n' '#!/bin/sh' 'for d in /workspaces/cloudultron /workspaces/Cloudultron; do if [ -d "$d/src/cloudultron" ]; then export PYTHONPATH="$d/src${PYTHONPATH:+:$PYTHONPATH}"; break; fi; done' 'exec python3 -m cloudultron "$@"' > /usr/local/bin/cloudultron && chmod 755 /usr/local/bin/cloudultron

RUN git config --system --add safe.directory /workspaces/cloudultron && git config --system --add safe.directory /workspaces/Cloudultron && git config --system --add safe.directory '*'

# Tool container for the Cloudultron sandbox: an editor attach point with `adb`
# and the repo's own interpreter requirements (which is to say, none -- the
# project is stdlib-only by design).
#
# Deliberately *not* the emulator image: budtmo/docker-android is Ubuntu + QEMU +
# an AVD, and building dev tooling on top of it means every change to this file
# rebuilds a multi-gigabyte layer. Splitting them also keeps the honest property
# that the executor talks to a device over a socket rather than living next to it.
FROM mcr.microsoft.com/devcontainers/base:ubuntu

ENV DEBIAN_FRONTEND=noninteractive

# `adb` is the Ubuntu binary package; `android-tools-adb` is the Debian *virtual*
# name for it. The base here is Ubuntu, where asking for android-tools-adb by name
# fails, so try Ubuntu's name first and fall back for anyone who rewrites the
# base image to Debian. Either provides /usr/bin/adb, which is all the transport
# needs -- no Android SDK, no platform-tools download, no emulator here.
RUN apt-get update \
    && (
        apt-get install -y --no-install-recommends adb \
        || apt-get install -y --no-install-recommends android-tools-adb \
    ) \
    && apt-get install -y --no-install-recommends python3 python3-venv \
    && (
        apt-get install -y --no-install-recommends docker.io \
        || echo "no docker client in this image: emulator logs need 'docker compose logs android' from the host instead" \
    ) \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# The docker CLI is for looking at the *sibling* container: when the emulator does
# not come up, the only useful evidence is `docker logs` of it, and that needs the
# socket (mounted in devcontainer.json) plus a client. Only the client is used;
# no daemon is enabled or started in this container. The install is wrapped in a
# fallback on purpose: it is diagnostics plumbing, and losing it should not be a
# build failure that also takes adb and python3 with it.

# `python3 run_tests.py` is the whole test interface, and the package is imported
# from src/, so an editable install would buy nothing except a dependency on a
# path that only exists once the volume is mounted. This wrapper is what lets the
# README's `cloudultron doctor` lines be typed as written; the path it exports is
# duplicated in compose's PYTHONPATH on purpose, so the command also works for
# anyone who runs the image without the dev container tooling.
RUN printf '%s\n' \
      '#!/bin/sh' \
      'export PYTHONPATH="/workspaces/cloudultron/src${PYTHONPATH:+:$PYTHONPATH}"' \
      'exec python3 -m cloudultron "$@"' \
      > /usr/local/bin/cloudultron \
    && chmod 755 /usr/local/bin/cloudultron

# The repo arrives as a mount owned by root while this container runs as `vscode`.
# Without this, every git command dies on "detected dubious ownership" -- and
# `--system` rather than `--global` because the image user's home is not where the
# editor will write its config.
RUN git config --system --add safe.directory /workspaces/cloudultron \
    && git config --system --add safe.directory /workspaces/Cloudultron \
    && git config --system --add safe.directory '*'

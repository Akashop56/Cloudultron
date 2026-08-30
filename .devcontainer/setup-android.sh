#!/usr/bin/env bash
# Bring the sandbox's Android device to a known state, then prove the executor can
# actually read it -- which is a different claim than "the container is running".
#
#   .devcontainer/setup-android.sh            wait for boot, verify, print next steps
#   .devcontainer/setup-android.sh --probe     answer immediately, for postStartCommand
#
# Exit status is deliberately asymmetric. A device that never answered because the
# host cannot virtualise at all is an environment limitation, not a broken repo:
# that path warns and returns 0, because failing here would mark the whole dev
# container as failed and take the editor and the test suite down with it. A
# device that answered and then failed to boot is a real fault, and returns 1 so
# nobody reads a green checkmark off a half-built sandbox.
set -uo pipefail

serial="${CLOUDULTRON_ADB_SERIAL:-android:5555}"
wait_seconds="${ANDROID_WAIT_SECONDS:-420}"
probe_only=0
[ "${1:-}" = "--probe" ] && probe_only=1

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="${repo_root}/src${PYTHONPATH:+:${PYTHONPATH}}"

log() { printf '[android] %s\n' "$*"; }

host="${serial%%:*}"
port="${serial##*:}"

tcp_alive() {
    timeout 3 bash -c "exec 3<>/dev/tcp/${host}/${port}" 2>/dev/null
}

# adb prints "unable to connect" / "failed to connect" on stderr while still
# exiting 0 in some versions, so the text is what decides, not the status.
adb_ready() {
    local out
    out="$(adb -s "$serial" shell getprop sys.boot_completed 2>&1 | tr -d '\r[:space:]')"
    [ "$out" = "1" ]
}

android_logs() {
    command -v docker >/dev/null 2>&1 || { log "   (no docker client; run: docker compose -f .devcontainer/docker-compose.yml logs android)"; return; }
    local cid
    cid="$(docker ps --filter 'name=android' --format '{{.Names}}' 2>/dev/null | head -1)"
    if [ -z "$cid" ]; then
        log "   the android container is not running at all -- 'docker ps -a' will say why it exited"
        return
    fi
    log "   last lines from ${cid}:"
    docker logs --tail 15 "$cid" 2>&1 | sed 's/^/[android]    | /'
}

explain_no_device() {
    log "no adbd answering on ${serial}."
    log "the usual cause is a host without /dev/kvm: the emulator image is QEMU and"
    log "refuses to run unaccelerated unless told otherwise. GitHub Codespaces does"
    log "not document nested virtualisation for its VMs, so a Codespace may well land"
    log "here. Two ways forward:"
    log "  1. work against the built-in fake device, which needs nothing:"
    log "       python3 -m cloudultron doctor --mock"
    log "       python3 run_tests.py"
    log "  2. get KVM: run this repo's dev container on a Linux host (or a"
    log "     self-hosted runner) that exposes /dev/kvm, or set"
    log "     EMULATOR_ADDITIONAL_ARGS=-no-accel in .devcontainer/.env and wait a"
    log "     long time. Option 2 is for bring-up only; do not develop against it."
    android_logs
}

if [ "$probe_only" -eq 1 ]; then
    if tcp_alive && adb_ready; then
        log "device ${serial} is up and booted"
        exit 0
    fi
    log "device ${serial} is not ready yet (probe only -- run .devcontainer/setup-android.sh to wait for it)"
    exit 0
fi

log "waiting for ${host}:${port}"
deadline=$(( $(date +%s) + wait_seconds ))
saw_tcp=0
while :; do
    if tcp_alive; then
        saw_tcp=1
        adb connect "$serial" >/dev/null 2>&1
        if adb_ready; then
            log "device booted after $(( wait_seconds - (deadline - $(date +%s)) ))s"
            break
        fi
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
        if [ "$saw_tcp" -eq 1 ]; then
            log "the port opened but sys.boot_completed never reached 1."
            log "that is a real emulator failure, not a missing feature -- the AVD"
            log "is probably still creating itself (first boot on shared cores is"
            log "slow) or it crashed mid-boot. Re-run this script with"
            log "ANDROID_WAIT_SECONDS=900 before believing anything else."
            android_logs
            exit 1
        fi
        explain_no_device
        exit 0
    fi
    sleep 5
done

# The claim worth making is about this repo: the device is not "up", it is
# parseable. doctor --deep runs the real dump -> parse -> focus path, so a
# half-initialised screen that would blow up later shows up now.
log "verifying with the executor itself"
if python3 -m cloudultron doctor --deep; then
    log "next:"
    log "  cloudultron snapshot --tree           # what the model would see"
    log "  cloudultron run --policy explore --steps 8"
    log "  open the noVNC preview to watch it happen"
else
    log "the device is booted but the executor could not read it -- see the error above."
    exit 1
fi

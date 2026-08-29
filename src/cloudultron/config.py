"""Runtime configuration for the executor.

Three ways to set it, in increasing precedence:

1. defaults in :class:`ExecutorConfig`
2. environment variables (``CLOUDULTRON_*``, plus ``ANDROID_SERIAL``)
3. command-line flags

Environment variables matter because on Termux you typically want the target
wired up once in ``~/.bashrc`` rather than repeated on every invocation::

    export CLOUDULTRON_ADB_SERIAL=192.168.1.50:5555
    export CLOUDULTRON_EXECUTION=execute   # dry_run is the default, not this
"""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass, field
from typing import Any, Mapping


#: Default TCP endpoint for an emulator reachable over adb over WiFi.
DEFAULT_REMOTE_SERIAL = "127.0.0.1:5555"

#: Where ``uiautomator dump`` writes on the device before we read it back.
DEVICE_DUMP_PATH = "/sdcard/window_dump.xml"


@dataclass
class ExecutorConfig:
    """Everything the loop needs that is not a decision.

    Notes on the anti-loop numbers
    -----------------------------
    These are *tuning*, not decoration. ``stagnation_limit`` and
    ``oscillation_max_period`` are what make the harness terminate instead of
    clicking the same coordinate for three hours, so they are exposed here on
    purpose rather than buried in the loop.
    """

    # --- transport ---------------------------------------------------
    #: adb target. A bare USB/emu serial ("emulator-5554"), or a network
    #: endpoint ("192.168.1.50:5555"), in which case ``adb connect`` is tried.
    serial: str | None = None
    adb_path: str = "adb"
    #: Per-command wall-clock budget. ``uiautomator dump`` on a slow device
    #: plus a big view tree can exceed 10s, hence the generous default.
    command_timeout: float = 30.0
    #: Retries for transient transport faults (device offline/unauthorized).
    transport_retries: int = 2
    #: Seconds to wait between transport retries.
    retry_backoff: float = 1.5

    # --- execution mode --------------------------------------------------
    #: True means: observe freely, *plan* mutations, never dispatch them.
    #: This is the default. Flipping it off is an explicit act.
    dry_run: bool = True
    #: Allow ``Guard`` to classify and block commands (see safety.py). There is
    #: intentionally no way to turn this off; you can only widen the allowlist.
    allow_destructive: bool = False

    # --- the loop --------------------------------------------------------
    max_steps: int = 25
    #: Pause after a dispatched mutation so the UI can settle before we re-look.
    settle_delay: float = 0.75
    #: ``wait_for_stable`` budget, for screens that load lazily.
    stability_timeout: float = 8.0
    #: Consecutive steps with an identical structure hash before we call it stuck.
    stagnation_limit: int = 4
    #: Largest cycle length treated as oscillation (A-B-A-B is period 2).
    oscillation_max_period: int = 3
    #: Same (state, action) pair this many times means the *policy* is spinning,
    #: which is different from the *screen* being stuck.
    policy_livelock_limit: int = 3

    # --- observation -----------------------------------------------------
    #: Cap on nodes handed to the policy digest, to bound token/scroll cost.
    max_interactables: int = 60
    #: Truncate element labels in the digest.
    max_label_len: int = 48
    #: If True, a step whose dump failed is fatal instead of retry-and-continue.
    fail_fast_on_dump_error: bool = False

    # --- output ------------------------------------------------------
    record_dir: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    # ---------------------------------------------------------------- env

    @classmethod
    def from_env(cls, overrides: Mapping[str, Any] | None = None) -> "ExecutorConfig":
        """Build config, letting ``CLOUDULTRON_*`` and ``ANDROID_SERIAL`` intrude."""
        env = os.environ
        cfg = cls()

        serial = env.get("CLOUDULTRON_ADB_SERIAL") or env.get("ANDROID_SERIAL")
        if serial:
            cfg.serial = serial.strip() or None

        if env.get("CLOUDULTRON_ADB_PATH"):
            cfg.adb_path = env["CLOUDULTRON_ADB_PATH"].strip()

        mode = (env.get("CLOUDULTRON_EXECUTION") or "").strip().lower()
        if mode in {"execute", "real", "live"}:
            cfg.dry_run = False
        elif mode in {"dry-run", "dry_run", "dry", "plan"}:
            cfg.dry_run = True

        for var, attr, caster in (
            ("CLOUDULTRON_MAX_STEPS", "max_steps", int),
            ("CLOUDULTRON_SETTLE_DELAY", "settle_delay", float),
            ("CLOUDULTRON_COMMAND_TIMEOUT", "command_timeout", float),
            ("CLOUDULTRON_STAGNATION_LIMIT", "stagnation_limit", int),
            ("CLOUDULTRON_MAX_INTERACTABLES", "max_interactables", int),
        ):
            raw = env.get(var)
            if raw:
                try:
                    setattr(cfg, attr, caster(raw.strip()))
                except ValueError:  # a typo'd env var should not be cryptic
                    raise ValueError(f"{var}={raw!r} is not a valid {caster.__name__}") from None

        if overrides:
            for key, value in overrides.items():
                if value is None:
                    continue  # an unset flag must not clobber an env value
                if not hasattr(cfg, key):
                    raise AttributeError(f"ExecutorConfig has no field {key!r}")
                setattr(cfg, key, value)
        return cfg

    # --------------------------------------------------------------- misc

    @property
    def is_remote_serial(self) -> bool:
        """True when the serial looks like ``host:port`` and needs ``adb connect``."""
        if not self.serial:
            return False
        host, _, port = self.serial.rpartition(":")
        return bool(host) and port.isdigit()

    def redacted(self) -> dict[str, Any]:
        """Config as plain data, for logging at the start of every run.

        Nothing in this dataclass is secret today, but routing all config
        logging through here keeps a future credential field from leaking into
        a recorded trace file.
        """
        data = dataclasses.asdict(self)
        data.pop("extra", None)
        return data

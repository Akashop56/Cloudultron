"""Structural checks on the Codespaces / Docker-Android sandbox config.

There is no YAML parser available to a stdlib-only project, so this file reads
the compose document the way the repo reads its other config: a small, explicit
line scanner, and assertions only on the handful of fields that actually carry
meaning for the executor. That is deliberate -- the point is not to validate
Docker Compose, it is to keep four contracts from drifting:

1. the emulator is reachable at the serial ``ExecutorConfig`` is taught to read,
2. the dev container attaches to the service the compose file actually defines,
3. the workspace path matches between the mount and ``workspaceFolder``,
4. the boot-wait script distinguishes "no virtualisation here" from "the emulator
   is broken", because those two need opposite exit statuses.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import socket
import subprocess
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
DEVCONTAINER = REPO / ".devcontainer"


# ------------------------------------------------------------ tiny readers


def strip_jsonc(text: str) -> str:
    """Remove ``//`` + ``/* */`` comments and trailing commas from JSONC.

    devcontainer.json is JSONC and both supported tools accept comments, which is
    why they are used there. A state machine rather than a regex because a naive
    ``//`` strip eats the slashes in ``http://`` inside a string value.
    """
    out: list[str] = []
    i, n = 0, len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            if ch == "\\":  # keep the escaped pair whole
                out.append(text[i : i + 2])
                i += 2
                continue
            if ch == '"':
                in_string = False
            out.append(ch)
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end == -1 else end
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i)
            i = n if end == -1 else end + 2
            continue
        out.append(ch)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def load_devcontainer() -> dict:
    return json.loads(strip_jsonc((DEVCONTAINER / "devcontainer.json").read_text(encoding="utf-8")))


def _lines(text: str):
    for raw in text.splitlines():
        stripped = raw.split("#", 1)[0].rstrip() if not raw.lstrip().startswith("#") else ""
        if stripped.strip():
            yield len(stripped) - len(stripped.lstrip()), stripped.strip()


def compose_body(text: str, service: str) -> list[tuple[int, str]]:
    """The indented body lines of ``services.<service>``, empty if absent."""
    lines = list(_lines(text))
    try:
        top = next(i for i, (ind, val) in enumerate(lines) if ind == 0 and val == "services:")
    except StopIteration:
        return []
    out: list[tuple[int, str]] = []
    found = False
    for indent, value in lines[top + 1 :]:
        if indent <= 2:
            if found:
                break  # the next service, or the next top-level key
            if value.endswith(":"):
                found = value[:-1] == service
                continue
        if found:
            out.append((indent, value))
    return out


def compose_scalar(body: list[tuple[int, str]], key: str) -> str | None:
    for indent, value in body:
        if value.startswith(f"{key}:") and indent == 4:
            return value.split(":", 1)[1].strip().strip("\"'")
    return None


def compose_seq(body: list[tuple[int, str]], key: str) -> list[str]:
    """Items of a block sequence under ``key`` (ports, volumes, depends_on...)."""
    items: list[str] = []
    collecting = False
    for indent, value in body:
        if indent == 4 and value.startswith(f"{key}:"):
            collecting, items = True, []
            continue
        if not collecting:
            continue
        if indent <= 4:
            break
        if value.startswith("- "):
            items.append(value[2:].strip().strip("\"'"))
    return items


def compose_children(body: list[tuple[int, str]], key: str) -> dict[str, str]:
    """Mapping under ``key`` (environment, build...)."""
    out: dict[str, str] = {}
    collecting = False
    for indent, value in body:
        if indent == 4 and value.startswith(f"{key}:"):
            collecting, out = True, {}
            continue
        if not collecting:
            continue
        if indent <= 4:
            break
        if ":" in value:
            name, _, val = value.partition(":")
            out[name.strip()] = val.strip().strip("\"'")
    return out


# ------------------------------------------------------- the four contracts


class ComposeFileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = (DEVCONTAINER / "docker-compose.yml").read_text(encoding="utf-8")
        cls.android = compose_body(cls.text, "android")
        cls.dev = compose_body(cls.text, "dev")

    def test_image_and_vnc_are_the_documented_names(self):
        # The env var names come from the image's own contract; inventing one is a
        # silent no-op that shows up as "the web UI never appears".
        self.assertIn("budtmo/docker-android", compose_scalar(self.android, "image") or "")
        self.assertEqual(compose_children(self.android, "environment").get("WEB_VNC"), "true")
        self.assertEqual(compose_children(self.android, "environment").get("WEB_VNC_PORT"), "6080")

    def test_device_profile_is_one_the_image_ships(self):
        # EMULATOR_DEVICE must match a built-in profile exactly; a typo gives a
        # container that exits, which reads as an infrastructure failure.
        supported = {
            "Samsung Galaxy S10", "Samsung Galaxy S9", "Samsung Galaxy S8",
            "Samsung Galaxy S7 Edge", "Samsung Galaxy S7", "Samsung Galaxy S6",
            "Nexus 4", "Nexus 5", "Nexus One", "Nexus S", "Nexus 7", "Pixel C",
        }
        self.assertIn(compose_children(self.android, "environment").get("EMULATOR_DEVICE"), supported)

    def test_adb_and_novnc_ports_are_published(self):
        published = {item.split(":")[0] for item in compose_seq(self.android, "ports")}
        self.assertEqual({"6080", "5555"} - published, set(), published)

    def test_kvm_arrives_by_privilege_not_by_a_device_line(self):
        # `devices: ["/dev/kvm"]` is the textbook setting and the wrong one here:
        # compose fails the *whole* project at create time when the host has no
        # /dev/kvm, which also kills the dev container and hides the reason.
        self.assertEqual(compose_scalar(self.android, "privileged"), "true")
        # Checked structurally, not by grepping the raw text: the prose above the
        # line explains this very choice, and a substring test would trip over it.
        self.assertIsNone(compose_scalar(self.android, "devices"))

    def test_no_obsolete_version_key(self):
        # A `version:` line is ignored by Compose v2 and warns; the devcontainer
        # tool surfaces that warning as if the config were at fault.
        self.assertNotRegex(self.text, r"(?m)^version:")

    def test_the_serial_comes_from_compose_not_from_the_editor_config(self):
        # One source for the serial, and it is the one that also applies to a bare
        # `docker compose up`, where no devcontainer tooling is involved at all.
        self.assertIn("CLOUDULTRON_ADB_SERIAL", compose_children(self.dev, "environment"))
        self.assertNotIn("containerEnv", json.loads(strip_jsonc((DEVCONTAINER / "devcontainer.json").read_text())))

    def test_the_emulator_container_keeps_its_device_state_on_restart(self):
        volumes = compose_seq(self.android, "volumes")
        self.assertTrue(any(v.endswith(":/home/androidusr") for v in volumes), volumes)


class YamlSoundnessTests(unittest.TestCase):
    """Catch the class of error that is invisible until someone runs compose.

    There is no docker and no YAML library here, which is precisely the situation
    a reviewer is in too: a stray tab or a duplicated key would not fail a test,
    a build, or a lint -- it would fail at container-create time in a cloud VM,
    forty minutes into someone's day. These rules are the subset that a
    line-oriented config of this shape can actually violate.
    """

    KEY = re.compile(r"^[A-Za-z0-9_.-]+:( .*)?$")
    ITEM = re.compile(r"^- .+$")

    def _entries(self):
        text = (DEVCONTAINER / "docker-compose.yml").read_text(encoding="utf-8")
        for number, raw in enumerate(text.splitlines(), start=1):
            body = raw.split("#", 1)[0].rstrip()
            if body.strip():
                yield number, body

    def test_no_tabs_and_indentation_steps_by_two(self):
        for number, line in self._entries():
            indent = len(line) - len(line.lstrip())
            self.assertNotIn("\t", line, f"line {number}: YAML forbids tab indentation")
            self.assertEqual(indent % 2, 0, f"line {number}: indent {indent} is not a multiple of 2")

    def test_every_line_is_a_key_or_a_sequence_item(self):
        for number, line in self._entries():
            body = line.strip()
            self.assertTrue(
                self.KEY.match(body) or self.ITEM.match(body),
                f"line {number} is neither `key: value` nor `- item`: {body!r}",
            )
            self.assertEqual(body.count('"') % 2, 0, f"line {number}: unbalanced quote")

    def test_keys_are_unique_within_each_block(self):
        # Duplicated keys in one mapping silently drop the first value, which in
        # this file would mean a published port or an env var vanishing. Note the
        # scope is a *block*, not a key name: `volumes:` appears under both
        # services and that is correct, so identity has to include the path.
        stack: list[tuple[int, str]] = []
        seen: dict[tuple[tuple[str, ...], str], int] = {}
        for number, line in self._entries():
            indent = len(line) - len(line.lstrip())
            body = line.strip()
            while stack and indent <= stack[-1][0]:
                stack.pop()
            if body.startswith("- "):
                continue  # sequence items are not keys
            key = body.split(":", 1)[0]
            path = tuple(name for _, name in stack)
            prior = seen.get((path, key))
            self.assertIsNone(
                prior,
                f"line {number} re-declares {key!r} inside {'/'.join(path) or '<root>'}"
                f" (first at line {prior}); the earlier value would be discarded",
            )
            seen[(path, key)] = number
            stack.append((indent, key))

    def test_port_mappings_are_quoted(self):
        # Unquoted `6060:6060` is a valid YAML *integer* in the 1.1 sexagesimal
        # dialect some loaders use; the quoting convention is the guard, so pin it.
        for number, line in self._entries():
            body = line.strip()
            if body.startswith("- ") and re.match(r"^- \d+:\d+", body):
                self.assertTrue(body.startswith('- "'), f"line {number}: quote port mappings: {body!r}")


class DevContainerJsonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = load_devcontainer()
        cls.compose = (DEVCONTAINER / "docker-compose.yml").read_text(encoding="utf-8")

    def test_it_parses_and_attaches_to_a_service_that_exists(self):
        self.assertEqual(self.cfg["dockerComposeFile"], "docker-compose.yml")
        service = self.cfg["service"]
        self.assertTrue(compose_body(self.compose, service), f"service {service!r} is not in the compose file")

    def test_workspace_folder_matches_the_mount_target(self):
        # A mismatch means the editor opens an empty directory while the shell,
        # silently, is somewhere else -- and the postCreate script then fails in
        # a way that looks like a broken repo.
        dev_body = compose_body(self.compose, self.cfg["service"])
        mount = compose_seq(dev_body, "volumes")[0]
        self.assertTrue(mount.endswith(":" + self.cfg["workspaceFolder"] + ":cached"), mount)
        self.assertEqual(compose_scalar(dev_body, "working_dir"), self.cfg["workspaceFolder"])

    def test_no_dependency_features_are_declared(self):
        # Features are not reliably applied to compose services across tools, and
        # a partially-applied feature is worse than none. ./Dockerfile owns this.
        self.assertNotIn("features", self.cfg)
        self.assertTrue((DEVCONTAINER / "Dockerfile").exists())

    def test_ports_the_sandbox_needs_are_forwarded(self):
        self.assertEqual(sorted({6080, 5555} & set(self.cfg["forwardPorts"])), [5555, 6080])

    def test_lifecycle_scripts_run_in_the_right_order(self):
        # postCreate, not onCreate: the script imports the package from src/, and
        # onCreate runs before the repository contents are copied into the
        # container.
        self.assertIn("setup-android.sh", self.cfg["postCreateCommand"])
        self.assertNotIn("onCreateCommand", self.cfg)
        self.assertIn("--probe", self.cfg["postStartCommand"])

    def test_the_machine_floor_is_declared_for_codespaces_only(self):
        host = self.cfg["hostRequirements"]
        self.assertGreaterEqual(host["cpus"], 4, "a 2-core machine boots an AVD in roughly never")
        self.assertIn("memory", host)

    def test_adb_package_names_are_both_tried(self):
        # Ubuntu has `adb`, Debian has the `android-tools-adb` virtual name. Pinning
        # only the name from the ticket would build fine here and fail on Debian.
        dockerfile = (DEVCONTAINER / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("apt-get install -y --no-install-recommends adb", dockerfile)
        self.assertIn("android-tools-adb", dockerfile)


class SerialWiringTests(unittest.TestCase):
    """The compose-supplied serial must be the one this project's config honours."""

    def test_compose_serial_is_read_by_the_config_and_treated_as_network(self):
        from cloudultron.config import ExecutorConfig

        compose = (DEVCONTAINER / "docker-compose.yml").read_text(encoding="utf-8")
        serial = compose_children(compose_body(compose, "dev"), "environment")["CLOUDULTRON_ADB_SERIAL"]

        saved = os.environ.get("CLOUDULTRON_ADB_SERIAL")
        os.environ["CLOUDULTRON_ADB_SERIAL"] = serial
        try:
            cfg = ExecutorConfig.from_env()
        finally:
            if saved is None:
                del os.environ["CLOUDULTRON_ADB_SERIAL"]
            else:
                os.environ["CLOUDULTRON_ADB_SERIAL"] = saved

        self.assertEqual(cfg.serial, serial)
        self.assertTrue(cfg.is_remote_serial, "host:port is what makes the transport run `adb connect`")
        self.assertNotIn("5555", serial.split(":")[0], "the host must be the service name, not a literal IP")

    def test_the_package_imports_in_the_dev_container_without_installing(self):
        # The Dockerfile's wrapper sets PYTHONPATH to src/; prove that path is the
        # real one so `cloudultron doctor` is not a broken symlink waiting to happen.
        dockerfile = (DEVCONTAINER / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("/workspaces/cloudultron/src", dockerfile)
        self.assertTrue((REPO / "src" / "cloudultron" / "__main__.py").exists())


class SetupScriptTests(unittest.TestCase):
    """The asymmetry in the wait script is its whole purpose, so it is tested."""

    def run_script(self, serial: str, *args: str) -> subprocess.CompletedProcess:
        env = dict(os.environ, CLOUDULTRON_ADB_SERIAL=serial, ANDROID_WAIT_SECONDS="0")
        return subprocess.run(
            ["bash", str(DEVCONTAINER / "setup-android.sh"), *args],
            cwd=REPO, env=env, capture_output=True, text=True, timeout=60,
        )

    def test_missing_virtualisation_warns_without_failing_the_container(self):
        # Nothing listening: this is the "host has no KVM" shape, and a red X on
        # the dev container would take the editor and the test suite with it.
        proc = self.run_script("127.0.0.1:1")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("/dev/kvm", proc.stdout)
        self.assertIn("--mock", proc.stdout)

    def test_a_device_that_answers_but_never_boots_is_an_error(self):
        # Something listening, boot never completes: a real fault, so it must not
        # look like the same tolerated environment limitation.
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            proc = self.run_script(f"127.0.0.1:{port}")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("sys.boot_completed", proc.stdout)

    def test_probe_mode_never_waits_and_never_fails(self):
        # postStartCommand runs on every container start, including one where the
        # emulator was intentionally left down; it may report, not block or scold.
        proc = self.run_script("127.0.0.1:1", "--probe")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("not ready", proc.stdout)

    def test_script_is_executable_and_bash_clean(self):
        script = DEVCONTAINER / "setup-android.sh"
        self.assertTrue(os.access(script, os.X_OK), "git must keep the exec bit or ./ invocation breaks")
        self.assertIn("set -uo pipefail", script.read_text(encoding="utf-8"))
        check = subprocess.run(
            ["bash", "-n", str(script)], capture_output=True, text=True, timeout=30
        )
        self.assertEqual(check.returncode, 0, check.stderr)


class DocsTests(unittest.TestCase):
    def test_readme_documents_the_sandbox_and_its_files(self):
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        self.assertIn("devcontainer", readme.lower())
        for name in ("docker-compose.yml", "devcontainer.json", "Dockerfile", "setup-android.sh"):
            self.assertIn(name, readme, f"{name} is not mentioned in the README")
            self.assertTrue((DEVCONTAINER / name).exists(), f"{name} is documented but missing")

    def test_no_secrets_in_committed_config(self):
        for path in sorted(DEVCONTAINER.iterdir()):
            if path.suffix not in {".json", ".yml", ".sh"}:
                continue
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("VNC_PASSWORD=", text, "a VNC password belongs in .env, not in git")
            self.assertNotRegex(text, r"[\w.]+@[\w.]+\.(com|org|io)", "an email address does not belong here")

    def test_no_mount_pokes_at_a_specific_home_directory(self):
        # Container-internal paths (/home/androidusr is the image's own) are fine;
        # a *source* path like ~/projects/... is what breaks the next clone.
        compose = (DEVCONTAINER / "docker-compose.yml").read_text(encoding="utf-8")
        for service in ("android", "dev"):
            for item in compose_seq(compose_body(compose, service), "volumes"):
                source = item.rsplit(":", 2)[0]
                self.assertFalse(source.startswith(("~", "/")), f"{service}: {item!r} hardcodes a host path")
        for item in load_devcontainer().get("mounts", []):
            source = dict(kv.split("=", 1) for kv in item.split(",") if "=" in kv)
            self.assertIn(source.get("source", ""), {"/var/run/docker.sock"},
                          "the docker socket is the one bind Codespaces honours")


if __name__ == "__main__":
    unittest.main()

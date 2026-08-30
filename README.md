# Cloudultron

An Android UI automation **executor**: an observe → think → act loop with
structural state-diffing and a command guard that is enforced in code rather than
requested in a prompt.

Python 3.9+, **zero runtime dependencies** — the whole thing is `xml.etree`,
`hashlib`, `subprocess` and `dataclasses`, because it is meant to install on
Termux as happily as on a workstation.

```bash
PYTHONPATH=src python3 -m cloudultron doctor --mock    # no device needed
PYTHONPATH=src python3 -m cloudultron run --mock --policy explore
python3 run_tests.py                                    # 348 tests, stdlib only
```

---

## Why it is shaped this way

Four decisions carry most of the design, and each exists because the naive
version of it fails:

**1. Policies emit typed actions, not shell strings.**
The decision-maker returns `Action.tap(index=3)`. Only the dispatcher turns that
into `input tap 540 965`. So `rm -rf /sdcard` is not *forbidden* — it is
unrepresentable, which is a stronger property and needs less filtering. Raw
shell exists as one escape hatch (`Action.raw_shell`); whether a policy may use it
is one of the things a profile decides (see below), and every command that goes
through it is classified by argv token rather than by regex over a string.

**2. Every screen gets two hashes, not one.**
A screen is never byte-identical between reads: a clock ticks, a spinner adds a
node, a badge changes. Hash everything and every step looks like a transition, so
your loop never detects that it is stuck. Hash only geometry and you miss a login
screen that swapped in "wrong password" without moving a pixel.

| | ignores | answers | used for |
|---|---|---|---|
| `structure_hash` | text, content-desc | *is this a different screen?* | anti-loop trip, "did my tap do anything" |
| `content_hash` | nothing user-visible | *did anything the user can see change?* | stability waits, effect check |

**3. "The screen changed" is not the anti-loop signal.**
A ping-pong between two screens produces a legitimate change on *every step* —
each individual action worked — so change-detection declares it healthy forever.
The loop keeps three separate verdicts because they have different causes:

- `stagnation` — structure identical for N steps. The action had no effect.
- `oscillation` — the structure hash cycles with period 2..N, and a cycle needs at
  least two distinct screens: four identical hashes are stagnation. The policy
  needs a "do not come back here" rule; a frozen screen does not.
- `livelock` — the same `(screen, action)` **pair** recurs even while the screen
  changes. This one is a bug in *us*, and it is checked independently of dispatch,
  so it still fires in dry-run. `Action.key()` deliberately excludes the
  rationale, because the failure mode of an LLM policy is rewording the same
  bad idea every step while the check looks for an identical string.

**4. The guard is a dial, not a switch, and the dial has a floor.**
Whether a device is safe to point this at is a fact about the *device*, not about
this repository — so it cannot live in the code as a constant, and it cannot live
in the policy either, because the policy is the thing being checked. It is a
profile the operator selects, and it only ever relaxes what the strict default
permits. What no profile removes is classification and logging: a run that
executed `rm -rf /sdcard` while reporting nothing is a run you cannot read
afterwards. Deleting the check is what makes a copied file dangerous; arming it
off, in writing, on a device you own, is not.

---

## Layout

```
src/cloudultron/
  config.py     ExecutorConfig; env-var overrides (CLOUDULTRON_*, ANDROID_SERIAL)
  safety.py     Effect tiers (read < write < destructive) + Guard. The enforcement point.
  errors.py     raise on hard failure, return on policy outcome
  adb/
    transport.py  argv construction, quoting, exec-out, transient retries
    device.py     the verbs: hierarchy(), tap, swipe, text, keyevent, am/pm, focus
  ui/
    model.py      Rect, UiNode, Screen — incl. interactables() pruning
    parser.py     dump bytes -> tree; survives garbage, control bytes, truncation
    hashing.py    the two hashes, compare() -> Diff, LoopDetector
    render.py     compact index-addressable digest for a decision-maker
  loop/
    actions.py    the action vocabulary + Dispatcher (the only writer of state)
    policy.py     Policy protocol + Null / Scripted / Explore
    llm.py        the model-backed policy: prompt, urllib call, JSON -> Action
    engine.py     the loop, tripwires, budgets, trace
  testing/fake.py a fake that answers adb, so the stack runs with no device
  cli.py          doctor | snapshot | run | shell | script
```

Import direction is one way and the test suite enforces it: `ui` depends on
nothing but `errors`, `adb` sits on `ui`, `loop` sits on `ui`+`safety` and **never**
on `adb` — that is what keeps the executor testable against the fake.
See `tests/test_hygiene.py`.

---

## The loop, one step

```
observe   uiautomator dump -> parse -> structure_hash, content_hash, current_focus
think     diff vs previous; LoopDetector.check(); budget check      <- before decide
decide    policy.decide(Observation) -> Action                       (expensive)
act       Guard.check_*  ->  deferred? record the plan : dispatch     (cheap, decisive)
          the *profile* decides what is refused here; it never decides what the
          command was -- classification runs first and unconditionally, so the
          trace says what a stricter profile would have stopped
settle    sleep only when something was actually put on the wire
          device.invalidate()  <- without this, a post-tap read can be served a
                                  pre-tap dump and every action looks like a no-op
```

Checks run *before* the policy call on purpose: a step that is going to trip is
not worth a model round-trip.

---

## CLI

| | mutates the device? |
|---|---|
| `doctor [--deep]` | no |
| `snapshot [--save f] [--tree] [--compare-with f]` | no |
| `run [--policy observe\|explore\|scripted\|llm] [--steps N]` | autonomous policy: only with `--execute`. script: by default |
| `shell 'dumpsys window'` | read-only unless `--execute` |
| `script FILE [--emit]` | no |

The default mode follows the **provenance of the actions**, not a global setting:

* `--policy explore` / `observe` / `llm` — the actions came from a model or a
  wanderer that nobody reviewed, so dry-run stays on and `--execute` must be typed.
* `--policy scripted --script nav.txt` — you opened that file, so a replay
  dispatches by default; pass `--dry-run` to plan instead. Requiring `--execute`
  on every replay just teaches people to type it without reading.
* `--i-am-the-operator` — implies `--execute`, and says so on stderr first. So does
  naming the profile (`--guard-profile operator`, or the environment), because
  those are one decision written three ways; only an explicit `--dry-run` overrides
  it. `--guard-profile test-lab` does *not* imply execution: that profile changes
  what is blocked, not whether you meant to touch the device.

Every run prints the armed ruleset to stderr unless `-q`, so "which of these did
I set three flags ago" never needs memory.

```bash
# real device, local emulator
cloudultron run --serial emulator-5554 --policy explore --steps 20 --verbose

# adb over WiFi (host:port triggers `adb connect` for you)
cloudultron --serial 192.168.1.50:5555 snapshot

# record a replayable trace, then drive it in from CI
cloudultron run --execute --policy scripted --script nav.txt --record runs/ --json
```

Exit codes: `0` clean or step-budget reached, `3` a tripwire fired, `4` the guard
blocked/deferred, `1` device or transport error, `2` usage, `5` a shell command that
ran and failed, `130` interrupted.

---

## Guard profiles

The guard is one object with three settings. Pick the setting, not the code.

```bash
cloudultron run --guard-profile explore      # the default, and the strictest
cloudultron run --guard-profile test-lab     # a device you are paid to break
cloudultron run --i-am-the-operator          # nothing blocked; everything logged
```

| | what is refused | patterns enforced | a policy may compose shell |
|---|---|---|---|
| `explore` | every destructive verb (`rm`, `reboot`, `chmod`, `dd`, `mkfs`, …) | all of them | no |
| `test-lab` | the device-wrecking list only | always-on + `rm -rf` | yes, gated by content |
| `operator` | nothing | none (still classified) | yes |

`test-lab` exists for a farm device where `reboot`, `pm clear`, `am force-stop`,
`chmod`/`chown`, `settings put` and `pm uninstall` are Tuesday. It opens the raw
channel *because* a profile whose permitted verbs are unreachable from a run is
decoration; what stays shut is the content — `mkfs`, `fdisk`, `wipefs`, `fastboot`,
redirects into `/dev`, `curl … | sh`, `rm -rf` of a wide path, command
substitution. Those are refused in every profile except `operator`.

Two rules hold across all three:

1. **Classification is never optional.** Splitting on `&&`, `||`, `;` and `|`
   before tokenising means `ls && rm -rf /sdcard` is judged as the second segment,
   not the first. Operator mode turns off *gating*, not *reading*.
2. **Profiles only relax.** No named profile may block something the strict
   default allowed, so choosing one can never break a command that already worked.
   A test pins this.

`--allow VERB` releases one verb from the blocklist for a single invocation:

```bash
cloudultron run --guard-profile test-lab --allow rm --execute --policy explore
```

`--allow` widens a profile, it cannot install one: `--allow mkfs` is a usage error
outside operator mode, because a flag that reads like an exception should not be
the way you wipe a partition table.

### Clicking things the explorer would normally avoid

`--allow-label 'Buy now'` releases one label from the explorer's danger filter for
this suite. The filter is a heuristic for unsupervised wandering, not a security
boundary, so authorising a purchase or account-deletion flow you *meant* to test is
a suite-scope decision:

```bash
cloudultron run --policy explore --allow-label 'Buy now' --allow-label 'Delete account'
cloudultron run --policy explore --danger-label 'transfer'      # add bank-app words
cloudultron run --policy explore --no-danger-filter             # click anything addressable
```

Matching is substring on the label and extends rather than replaces, so adding
`transfer` does not quietly drop `delete`. `--no-danger-filter` is the blunt
version; prefer naming the labels.

### What operator mode guarantees you afterwards

Nothing is blocked, and that is the point, but the run is still legible: each
waived objection is written into the step (`operator override: <reason>`), counted
in the summary (`guard=operator(overrides=3)`), and repeated in `--json` under
`guard_log`. An unguarded run that cannot be read afterwards is not an operator
mode, it is just noise.

Environment equivalents for CI: `CLOUDULTRON_GUARD_PROFILE=test-lab`,
`CLOUDULTRON_OPERATOR=1`, `CLOUDULTRON_ALLOW_VERBS=chmod,rm`,
`CLOUDULTRON_ALLOW_LABELS=Buy now,Delete account`.

---

---

## Driving it with a model

`--policy llm` hands each step to a chat model. It is one module and no new
dependency: the request is `urllib.request`, the reply is JSON, so this works in the
sandbox and on Termux exactly as the rest of the project does.

```bash
export OPENROUTER_API_KEY=sk-or-v1-...            # the only way to hand it a key
cloudultron run --policy llm --goal "turn Wi-Fi on" --steps 12 --verbose
```

| env | default | notes |
|---|---|---|
| `OPENROUTER_API_KEY` | — | required. Read from the environment only: never an argument (`ps` and shell history see those), never in the trace |
| `LLM_MODEL` | `meta-llama/llama-3.1-70b-instruct:free` | any OpenAI-compatible model id |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | point it at a local server or a proxy; the call is `{base}/chat/completions` |

The request is the OpenAI-compatible shape the OpenRouter API documents — bearer auth,
`messages`, `temperature: 0`, a bounded `max_tokens`, and `response_format:
{"type": "json_object"}`. That last field is optional on the provider's side and is
dropped for one retry if an endpoint rejects it, because the JSON contract is restated
in the system prompt anyway: JSON mode is a convenience, not the parser's precondition.

The default is a `:free` model id and deliberately **not** `openrouter/auto`: the auto
router picks a model you did not choose, on a meter you did not set. `--policy llm`
also never implies `--execute` — a model's tap is *planned* until you say otherwise,
which is the same provenance rule the explorer and the script obey, read the other way.

Other flags: `--goal`, `--llm-model`, `--llm-timeout`, `--llm-max-tokens`,
`--llm-fallback` (when the provider fails, spend the step on the heuristic explorer
instead of erroring; those steps are labelled `fallback` in the trace, and a run never
keeps calling a dead endpoint).

**What it cost.** The reply's `usage` block is summed into a line at the end of the
run and into `report`'s `llm` key — requests, retries, failures, fallbacks, tokens,
and dollars when the server reports them. A non-free `LLM_MODEL` is echoed as `billed`
in the banner rather than refused: the default is a guardrail against surprise, not a
permission system. If a model id is served by something else, the summary names what
actually answered.

**What the model is told.** `Observation.to_prompt_dict()` — the indexed digest, the
diff since the last step, the last six actions with their outcomes, the remaining
budget, and the engine's own warnings — plus `loop_memory`, which is what the
observation cannot know by itself: which screen hashes have already been visited, which
actions are spent on this screen, which screens stopped responding. The system prompt
states the anti-loop rules those fields exist to enforce. That is on purpose: a prompt
that disagrees with `LoopDetector` would be a rule the run enforces sometimes and the
model believes always.

**What the model is allowed to ask for.** Everything in the action vocabulary, including
`raw_shell`. The policy translates it and hands it over; the armed profile decides
whether it reaches the device. Under `explore` that step is blocked and recorded; under
`test-lab` a `reboot` goes out. No second, invisible ruleset lives in the prompt, and
no "please be careful" is load-bearing anywhere in this file.

**How it fails.** A provider error becomes a `PolicyError`, the step is recorded as
refused, and three in a row end the run with `policy_errors` rather than spending the
remaining budget retrying. 429/5xx are retried twice with backoff (`Retry-After`
obeyed when sent); `temperature=0` and a bounded `max_tokens` keep a step reproducible
from the trace; a reply wrapped in a fence or in prose is still parsed, and the reason
a reply was rejected is fed to the next turn so a formatting slip costs one step
instead of a run.

All of that is tested without a key or the internet. `LLMPolicy(poster=...)` is the
seam that covers the prompt, the parser, the retry accounting and the guard interplay;
`RealHttpTests` additionally starts a loopback HTTP server so the actual
`urllib.request` call — headers on the wire, an `HTTPError` body read, a 429 retried —
is exercised too. What remains unverified here is the provider's own behaviour: that
`:free` endpoints exist, rate-limit and answer as documented is OpenRouter's side of
the contract, not something a test in this repo can hold them to.

## Sandbox: GitHub Codespaces + Docker-Android

The repo ships a dev container that runs the emulator next to the code, so the
executor talks to a real device over a socket instead of against the fake:

```
.devcontainer/
  devcontainer.json    which container the editor attaches to, ports, lifecycle
  docker-compose.yml   android (budtmo/docker-android) + dev (adb, python3)
  Dockerfile           the tool container: adb, python3, a `cloudultron` shim
  setup-android.sh     waits for boot, then proves the executor can read the screen
```

**One requirement decides everything: `/dev/kvm`.** `budtmo/docker-android` runs
QEMU, and QEMU on an x86 Android system image needs KVM. Check the host before you
blame the config:

```bash
ls -l /dev/kvm && kvm-ok          # "KVM acceleration can be used" is what you need
```

GitHub has never documented nested virtualisation for Codespaces VMs, so **assume
a Codespace may not have it** and read the first lines the container prints. The
setup script is written for exactly that case: a host that cannot virtualise warns
and exits 0 (the editor, the test suite and `--mock` all still work), while a
device that answers on 5555 and then fails to boot is a real fault and exits 1.

Inside the sandbox:

```bash
cloudultron doctor --deep               # adb + transport + dump + parse, all of it
cloudultron snapshot --tree             # what a policy would actually see
cloudultron run --policy explore --steps 8 --execute
docker logs --tail 50 "$(docker ps --filter name=android --format '{{.Names}}' | head -1)"
```

The serial is set once, in compose, as `CLOUDULTRON_ADB_SERIAL=android:5555`. A
`host:port` serial is what makes the transport run `adb connect` by itself, so no
step here needs `--serial`, and the same variable works from Termux or CI against
any other network target. `6080` is forwarded as the noVNC preview; open it and
watch your own loop press buttons.

Knobs, all in `.devcontainer/docker-compose.yml` (or `.env`, which is gitignored):

| | |
|---|---|
| Android version | image tag `emulator_9.0` … `emulator_14.0` (newer tags are the sponsored image) |
| Device profile | `EMULATOR_DEVICE`, and it must match the image's list verbatim |
| No KVM, still want it up | already the default: compose ships `EMULATOR_ADDITIONAL_ARGS` as `-no-accel`. It boots, slowly |
| KVM present | nothing to do; `initializeCommand` writes `.devcontainer/.env` with an empty value so acceleration is not traded away for the safe default |
| Boot patience | `ANDROID_WAIT_SECONDS=900` for the first cold AVD on shared cores |

The two lines above are the same value seen from two hosts, which is why neither is
hard-coded in the compose file: `${EMULATOR_ADDITIONAL_ARGS--no-accel}` (no colon, so
an *empty* value means "deliberately none" rather than "unset"). A committed
`-no-accel` would give every accelerated machine a twenty-fold slower boot and no
way to notice it.

The model provider's key passes through the same way — `OPENROUTER_API_KEY:
${OPENROUTER_API_KEY:-}` on the dev service, empty unless the host (or a
Codespaces secret) provides it. No credential is ever written into this repo.

The dev container also mounts the Docker socket, which is the one bind mount
Codespaces honours. That is what makes the last line above work: when the emulator
misbehaves, the evidence is in its logs, and the socket is how you get to them
without leaving the terminal.

Nothing in `src/` knows any of this exists — no `if in_container`, no special
transport. The sandbox is a device on a network, which is the only kind of
dependency worth committing.

## Termux

```bash
pkg install python android-tools
adb connect <emulator-host>:5555 && adb devices        # or a USB OTG device
git clone <this repo> && cd Cloudultron
PYTHONPATH=src python3 -m cloudultron doctor --deep
```

`adb` must be reachable from the shell you run in; on non-rooted devices
`uiautomator dump` and `input` work without a pairing prompt, but `am start`
to another user's app and `dumpsys` of some services will not. Everything here
degrades to "cannot observe" as a clean `HierarchyUnavailable` rather than a
half-read tree.

---

## Extending

A new profile is data, not code: a name, a verb blocklist, and a subset of the
`PATTERNS` keys, registered in `PROFILES`. Nothing else in the codebase changes,
which is the test that the seam is in the right place — if adding a ruleset needs
an `if` in the engine, the engine knows too much.

A policy is two methods:

```python
from cloudultron import Action, Executor, ExecutorConfig, build_device, Guard

class MyPolicy:
    name = "mine"

    def decide(self, obs) -> Action:
        # obs.digest is a compact, index-addressable screen listing;
        # obs.diff_summary says what changed since last step;
        # obs.history is the last few (action, outcome) pairs.
        if "[focused]" in obs.digest:
            return Action.text("hello")
        return Action.tap(index=2, rationale="Continue button")

policy, cfg = MyPolicy(), ExecutorConfig(dry_run=True)
device, _ = build_device(cfg)
Executor(device, policy, config=cfg).run()          # guard is built from cfg
```

An LLM-backed policy is just `decide()` returning `json.loads(model(observation.to_prompt_dict()))`
mapped onto `Action` factories. `to_prompt_dict()` exists so the payload sent to a
model is bounded and does not include raw XML.

`guard=` is optional: pass `guard_from_config(cfg)` or nothing, and the executor
builds the guard the config describes. The guard does not need to know about a new
policy — it only needs to know whether
an action *touches*. That is the payoff of the typed-action boundary.

## Status

Working: transport, parsing, hashing, diffing, all three tripwires, the guard and
its three profiles, provenance-based dry-run gating, trace recording, the fake
device, the CLI, 320 tests. The `.devcontainer/` sandbox needs `/dev/kvm` on the host
to go fast; without it the emulator still boots on software emulation, and everything
above runs against the fake device.

Not built yet, in rough order of usefulness: screenshot/OCR fallback for
`FLAG_SECURE` and canvas views; `--record` writing replayable scripts as well as
traces; `ADBKeyboard` for non-ASCII input (`input text` cannot encode it, so we
refuse instead of typing mojibake); multi-device fan-out. On the model side the suite
covers our half — prompt, parser, retries, provenance, and a real `urllib` call against
a loopback server — but not OpenRouter's half: that the `:free` id resolves, rate-limits
and answers as documented is a live-endpoint fact no test in this repo can hold them to.
See `## Driving it with a model`.

"""The model-backed policy: an OpenAI-compatible chat endpoint, stdlib only.

Everything about this class is a translation layer. The harness already knows how
to observe a screen, detect that it is going nowhere, and refuse an action it
should not send; what it could not do was *decide*. So this module does exactly
three things and no more:

1. serialise an :class:`Observation` into a bounded JSON prompt, and add the loop
   state the observation cannot know (which screens have already been visited,
   which actions on this one are already spent);
2. make one HTTP call with ``urllib.request``, with a bounded retry budget;
3. parse the reply into an :class:`Action`, or raise :class:`PolicyError`.

It deliberately does **not** gate anything. A model proposing ``raw_shell`` is
translated into an ``Action.raw_shell`` and handed to the engine, where the armed
profile decides -- which keeps the one rule this project is built on intact:
safety lives in the executor, not in the decision-maker or in a prompt.

The endpoint is OpenRouter by default because it is one URL, a bearer key, and a
`:free` tier, so a fresh checkout cannot accidentally start billing. Nothing here
imports a provider SDK: the whole request is 40 lines of ``urllib``, which is
what keeps this file installable on Termux.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from ..errors import PolicyError
from .actions import Action, Op
from .policy import ExplorePolicy, Observation

#: The key is read from here and from nowhere else. It is never accepted as a
#: command-line argument (argv leaks through ``ps`` and shell history), never
#: written to the trace, and never interpolated into an error message.
KEY_ENV = "OPENROUTER_API_KEY"
MODEL_ENV = "LLM_MODEL"
BASE_URL_ENV = "OPENROUTER_BASE_URL"

#: Deliberately a ``:free`` model id, and deliberately *not* ``openrouter/auto``:
#: the auto router picks by price/quality, so the cheapest safe default would
#: still be a model nobody chose and a bill nobody expected. Override with
#: $LLM_MODEL or --llm-model; a non-``:free`` id is reported, not refused.
DEFAULT_MODEL = "meta-llama/llama-3.1-70b-instruct:free"
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"

#: Optional, cosmetic, and documented twice under two names: OpenRouter's older
#: examples say ``X-Title``, the current ones ``X-OpenRouter-Title``. Sending both
#: costs a line and means the attribution lands whichever generation an endpoint
#: follows; harmless to a self-hosted OpenAI-compatible server, which ignores it.
ATTRIBUTION = {
    "HTTP-Referer": "https://github.com/Akashop56/Cloudultron",
    "X-Title": "cloudultron",
    "X-OpenRouter-Title": "cloudultron",
}

#: The output contract, in the model's terms. Kept as a tuple of strings so a test
#: can assert every rule is actually in the prompt instead of trusting prose.
RESPONSE_CONTRACT = (
    "Reply with ONE JSON object and nothing else: no prose, no markdown fence, no trailing comma.",
    'Shape: {"action": "<name>", "rationale": "<one short sentence>", ...arguments}.',
    'Actions: tap_index(index), tap_point(x, y), long_press(index), scroll(direction), '
    "text(value), keyevent(code), back, home, launch_app(package), start_activity(component), "
    "open_url(url), wait(seconds), done(rationale), abort(rationale).",
    "index is the number in square brackets in the screen digest, and must be one that is shown.",
)

#: The harness's own tripwires, phrased as instructions. These are not style
#: advice: each one corresponds to a check in ui/hashing.py that will end the run
#: if ignored, so the model is being told how to avoid a stop, not how to be tidy.
ANTILOOP_RULES = (
    "A screen is identified by its structure hash. If loop_memory.visited lists a hash you "
    "have already left, do not go back to it unless returning *is* the goal.",
    "Never choose an action that appears in loop_memory.spent_for_this_screen. Repeating an "
    "action on an unchanged screen is livelock: the harness counts it and aborts the run.",
    "If change or change_level says 'none', your last action did nothing here. Try a "
    "different control, or scroll; do not re-issue it with new wording.",
    "If hints mention oscillation, you are bouncing between screens: the way out is a "
    "control you have not tried on this screen, not 'back'.",
    "If loop_memory.dead_screens lists the current hash, nothing on it responds; go back.",
    "Say done the moment the goal is visible in the screen digest, and abort with a reason "
    "when no remaining action can make progress. Do not keep exploring a finished task.",
)

SYSTEM_PROMPT = (
    "You drive an Android device through a harness. You do not run commands; you choose one "
    "action per turn from the list below, and the harness performs it (or refuses it, if the "
    "operator's ruleset forbids that -- a refusal is information, not a failure).\n"
    "The screen is given as a flat digest: one line per addressable element, "
    "`[index] class label [flags]`, in top-to-bottom order. Only the indices shown exist.\n"
    "`recent_actions` is what you just did and what came of it. `loop_memory` is the "
    "harness's own anti-loop state; obey it.\n"
    "\n".join(f"- {rule}" for rule in ANTILOOP_RULES)
    + "\n\n"
    + "\n".join(f"- {rule}" for rule in RESPONSE_CONTRACT)
)

#: Names a model may use, mapped to the factory that builds the action. Both the
#: ``Op`` value and the script verb are accepted, because a model that has read
#: this project's README and one that has read a generic ADB tutorial will
#: describe the same tap with different words, and arguing with either is a
#: wasted step.
_ACTION_ALIASES: dict[str, Op] = {
    "noop": Op.NOOP,
    "tap": Op.TAP_INDEX,
    "tap_index": Op.TAP_INDEX,
    "click": Op.TAP_INDEX,
    "point": Op.TAP_POINT,
    "tap_point": Op.TAP_POINT,
    "long_press": Op.LONG_PRESS_INDEX,
    "long_press_index": Op.LONG_PRESS_INDEX,
    "scroll": Op.SCROLL,
    "swipe": Op.SWIPE,
    "text": Op.TEXT,
    "type": Op.TEXT,
    "keyevent": Op.KEYEVENT,
    "key": Op.KEYEVENT,
    "back": Op.BACK,
    "home": Op.HOME,
    "launch": Op.LAUNCH_APP,
    "launch_app": Op.LAUNCH_APP,
    "start_activity": Op.START_ACTIVITY,
    "open_url": Op.OPEN_URL,
    "url": Op.OPEN_URL,
    "wait": Op.WAIT,
    "done": Op.DONE,
    "finish": Op.DONE,
    "abort": Op.ABORT,
    "shell": Op.RAW_SHELL,
    "raw_shell": Op.RAW_SHELL,
}

_STATUS_WITH_RETRY = {429, 500, 502, 503, 504}


def _int(value: Any, field: str, obj: dict) -> int:
    """Coerce a model's number, which is often a string.

    "3" is a perfectly reasonable way for a language model to write an index and
    rejecting it wastes a step; "three" is not, and the message says so with the
    offending value in it so the next prompt can self-correct.
    """
    if isinstance(value, bool):
        raise PolicyError(f"{field} must be an integer, got {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("+").isdigit():
        return int(value.strip())
    if isinstance(value, float) and value.is_integer():
        return int(value)
    raise PolicyError(f"{field} must be an integer, got {value!r} (reply was {json.dumps(obj)[:160]})")


def _str(value: Any, field: str, obj: dict) -> str:
    if value is None:
        raise PolicyError(f"{field} is required")
    if not isinstance(value, str):
        value = str(value)
    if not value.strip():
        raise PolicyError(f"{field} must not be empty")
    return value.strip()


def extract_json(text: str) -> dict[str, Any]:
    """Pull the first *parseable* JSON object out of a model's reply.

    Models wrap. A fence is cosmetic and stripped; prose around the object is
    handled by trying each ``{`` in turn, honouring string escapes so a quote
    inside a rationale cannot end an object early. The first candidate that
    actually parses wins, which is the difference between shrugging off
    `Step {1}: I will tap it -- {"action":"back"}` and ending a run over a brace
    in a sentence. Accepting sloppily here costs nothing: every field the
    harness needs is validated afterwards, and an unusable object is still a
    PolicyError with the reason in it.
    """
    if not text or not text.strip():
        # Distinct from "no JSON object": an empty message means the call succeeded
        # and the model chose to say nothing, which usually means a content filter
        # or a max_tokens small enough to cut off before the first brace. The
        # operator needs to fix different things in each case.
        raise PolicyError("model returned an empty reply")
    body = text.strip()
    # A markdown fence needs no special handling: the scan below starts at the first
    # "{" and stops at its match, so ```json ... ``` is already transparent. Deleting
    # the pre-strip is deliberate -- two lines a test could not reach are two lines
    # that read as load-bearing and are not.
    starts = [index for index, char in enumerate(body) if char == "{"]
    if not starts:
        raise PolicyError(f"no JSON object in the reply: {body[:120]!r}")
    decode_error: str = ""
    truncated = False
    for start in starts:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(body)):
            char = body[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    candidate = body[start : index + 1]
                    try:
                        parsed = json.loads(candidate)
                    except json.JSONDecodeError as exc:
                        # Remember it, then look for a better opening brace: the
                        # first one may have been prose, as in `Step {1}: ...`.
                        decode_error = str(exc)
                        break
                    if isinstance(parsed, dict):
                        return parsed
                    decode_error = "top-level reply must be an object, not a list or scalar"
                    break
        else:
            # Fell off the end with braces still open -- a cut-off reply, and
            # worth reporting as such rather than as malformed JSON.
            truncated = True
    if truncated and not decode_error:
        raise PolicyError("unbalanced braces in the reply (it was probably truncated)")
    raise PolicyError(decode_error or "no complete JSON object in the reply")


@dataclass
class LoopMemory:
    """What the harness knows across steps that a stateless model does not.

    Recomputed from the observation each turn rather than remembered locally, so
    a policy that is restarted mid-run -- or a run whose trace is replayed --
    still gets the same prompt. Model context should be derived from the device's
    history, never from a private notebook that can drift out of sync with it.
    """

    current: str = ""
    visited: tuple[str, ...] = ()
    spent_for_this_screen: tuple[str, ...] = ()
    dead_screens: tuple[str, ...] = ()
    backfires: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "current_screen": self.current[:12],
            "visited": list(self.visited),
            "spent_for_this_screen": list(self.spent_for_this_screen),
            "dead_screens": list(self.dead_screens),
            "backfires": self.backfires,
        }

    @classmethod
    def from_observation(cls, observation: Observation, *, max_items: int = 8) -> "LoopMemory":
        current = observation.structure_hash
        history = list(observation.history or ())
        visits: dict[str, int] = {}
        spent: dict[str, set[str]] = {}
        for entry in history:
            key = (entry.structure_hash or "")[:12]
            if not key:
                continue
            visits[key] = visits.get(key, 0) + 1
            # Only an action that actually went to the device is evidence about a
            # screen. Counting a dry-run "planned" or a guard "blocked" step as a
            # spent attempt would tell the model this screen is dead when nothing
            # has ever been pressed on it -- which is how a preview run teaches
            # the policy to give up.
            if entry.outcome.startswith("executed"):
                spent.setdefault(key, set()).add(entry.action)
        # "dead" means the screen absorbed two *different* executed actions across
        # two or more visits without changing structure. One no-op is a misread or
        # an animation still running; two is a screen that does not answer.
        dead = tuple(key for key, actions in spent.items() if len(actions) >= 2 and visits.get(key, 0) >= 2)
        ranked = sorted(visits.items(), key=lambda item: (-item[1], item[0]))
        return cls(
            current=current,
            visited=tuple(f"{key}x{count}" for key, count in ranked if count)[:max_items],
            spent_for_this_screen=tuple(sorted(spent.get(current[:12], set())))[:max_items],
            dead_screens=dead[:max_items],
            backfires=sum(1 for e in history if e.outcome.startswith(("blocked", "refused", "failed"))),
        )


@dataclass
class LLMPolicy:
    """Decide the next action by asking a model, once per step.

    ``poster`` is the one seam that matters for testability: it is called with the
    finished request and must return ``(status, body_text)``. Injecting it keeps
    every parsing, retry and prompt rule testable without a network, which is how
    this file stays covered by ``python3 run_tests.py`` on a machine with no
    device and no internet.
    """

    #: The objective handed to the model. An LLM with no goal is just an expensive
    #: version of the explorer, and it will wander until the budget runs out.
    goal: str = "Explore the foreground app and report what is reachable, then say done."
    model: str = ""
    base_url: str = ""
    api_key: str = field(default="", repr=False)
    timeout: float = 45.0
    max_retries: int = 2
    max_tokens: int = 300
    temperature: float = 0.0
    #: When the provider fails, spend the step on the heuristic policy instead.
    #: Off by default: a run that silently stopped being LLM-driven is worse than
    #: a failed one, so this has to be asked for. Every such step is labelled
    #: ``fallback`` in the trace and counted in ``stats["fallbacks"]``.
    use_fallback: bool = False
    fallback: ExplorePolicy | None = None
    poster: Callable[[str, dict[str, str], bytes, float], tuple[int, str]] | None = None
    sleeper: Callable[[float], None] = field(default=time.sleep, repr=False)
    #: Billed steps, HTTP attempts, provider failures: reported by the CLI and
    #: asserted by tests, because "it talked to the model" is not verifiable
    #: after the fact without it.
    stats: dict[str, int] = field(default_factory=lambda: {"steps": 0, "requests": 0, "retries": 0, "failures": 0, "fallbacks": 0})
    #: What the run cost, read back from the provider's own `usage` block. The
    #: default model exists to keep this at zero, so the number belongs in the
    #: record rather than in someone's recollection of the pricing page.
    usage: dict[str, float] = field(default_factory=dict, repr=False)
    #: The model that actually served the last reply. On OpenRouter this can
    #: differ from the one asked for; a run should be able to show that.
    served_model: str = ""
    name: str = "llm"

    _last_rejection: str = ""
    _consecutive_failures: int = field(default=0, repr=False)
    _supports_json_mode: bool = field(default=True, repr=False)

    # -------------------------------------------------------------- wiring

    @classmethod
    def from_env(cls, *, goal: str | None = None, model: str | None = None, **kwargs: Any) -> "LLMPolicy":
        """Build from ``OPENROUTER_API_KEY`` / ``LLM_MODEL`` / ``OPENROUTER_BASE_URL``.

        Explicit arguments win, because a CLI flag that could not override the
        environment would make the environment impossible to test around.
        """
        key = (os.environ.get(KEY_ENV) or "").strip()
        if not key:
            raise PolicyError(
                f"{KEY_ENV} is not set, and --policy llm needs it. Export it in the shell "
                "or the container environment; do not put it in a file that gets committed, "
                "and do not pass it as an argument (argv is world-readable)."
            )
        chosen = (model or os.environ.get(MODEL_ENV) or DEFAULT_MODEL).strip() or DEFAULT_MODEL
        base = (os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL).strip().rstrip("/") or DEFAULT_BASE_URL
        extra: dict[str, Any] = {}
        if goal:
            extra["goal"] = goal
        return cls(model=chosen, base_url=base, api_key=key, **extra, **kwargs)

    def describe(self) -> str:
        """One line for the CLI banner. Never contains the key, or its length.

        The model id is echoed with a ``billed`` marker when it is not a free
        tier: the default exists to avoid surprise cost, so a run that has left it
        behind should say so once, plainly, before the first request goes out.
        """
        paid = ":free" not in self.model
        return (
            f"model={self.model}{' billed' if paid else ' free tier'} "
            f"endpoint={self.base_url} key=present "
            f"fallback={'on' if self.use_fallback else 'off (pass --llm-fallback)'}"
        )

    # ------------------------------------------------------------- the loop

    def build_prompt(self, observation: Observation) -> dict[str, Any]:
        """The exact request body, exposed so tests can assert on the prompt.

        ``to_prompt_dict`` is the bounded view; this adds the goal, the loop
        memory and -- when the last reply was unusable -- the reason it was
        rejected, which is what lets a model fix its own formatting on the next
        step instead of failing three times and ending the run.
        """
        payload = observation.to_prompt_dict()
        payload["goal"] = self.goal
        payload["loop_memory"] = LoopMemory.from_observation(observation).to_dict()
        if self._last_rejection:
            payload["rejected_last_reply"] = self._last_rejection
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "response_format": {"type": "json_object"},
        }

    def decide(self, observation: Observation) -> Action:
        self.stats["steps"] += 1
        try:
            reply = self._request(observation)
        except PolicyError as exc:
            self.stats["failures"] += 1
            self._consecutive_failures += 1
            if self.use_fallback:
                self.stats["fallbacks"] += 1
                driver = self.fallback or ExplorePolicy()
                self.fallback = driver
                borrowed = driver.decide(observation)
                # Copied rather than returned as-is: the trace has to show that a
                # heuristic chose this, and the reason the model did not.
                return replace(
                    borrowed,
                    rationale=f"{borrowed.rationale} [llm unavailable: {str(exc)[:90]}]",
                    source="fallback",
                )
            raise
        self._consecutive_failures = 0
        return self._to_action(reply, observation)

    def _request(self, observation: Observation) -> dict[str, Any]:
        if self._consecutive_failures >= 3:
            # Three strikes inside the policy, not just in the engine: without
            # this, a dead endpoint means one full retry budget per step, and a
            # 25-step run spends ten minutes rediscovering that the network is
            # down. The engine's own three-strike rule is for bad *decisions*.
            raise PolicyError(f"{self._consecutive_failures} consecutive provider failures; giving up on {self.base_url}")
        body = self.build_prompt(observation)
        if not self._supports_json_mode:
            body.pop("response_format", None)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            **ATTRIBUTION,
        }
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        url = f"{self.base_url}/chat/completions"
        poster = self.poster or self._post

        # One loop for both kinds of retry -- transport backoff and the
        # structured-output downgrade -- because the second is a *request shape*
        # change and recursing would re-enter the failure counter as if the
        # provider had failed a third time.
        attempt = 0
        downgraded = False
        while True:
            self.stats["requests"] += 1
            status, text = poster(url, headers, encoded, self.timeout)

            if status in _STATUS_WITH_RETRY and attempt < self.max_retries:
                attempt += 1
                self.stats["retries"] += 1
                self.sleeper(self._backoff(attempt, text))
                continue
            if (
                status in {400, 404, 422, 501}
                and not downgraded
                and "response_format" in body
                and "response_format" in text.lower()
            ):
                # Some open-weight endpoints reject structured-output support
                # outright. Drop the field once and continue -- retrying the same
                # body would fail identically, and the anti-loop rules in the
                # prompt are what actually keep the reply parseable.
                body.pop("response_format", None)
                encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
                downgraded = True
                self._supports_json_mode = False
                self.stats["retries"] += 1
                continue
            break

        if status >= 400:
            raise PolicyError(self._error_message(status, text))
        if status not in {200, 201}:
            raise PolicyError(f"provider returned HTTP {status}")
        return self._unwrap(text)

    def _record_usage(self, payload: dict[str, Any]) -> None:
        usage = payload.get("usage")
        if isinstance(usage, dict):
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                value = usage.get(key)
                if isinstance(value, (int, float)):
                    self.usage[key] = self.usage.get(key, 0) + value
            # `cost` is an OpenRouter extension, absent on other OpenAI-compatible
            # servers; a missing key must not look like a free run that was
            # actually unmeasured, so the two states are kept apart.
            if usage.get("cost") is not None:
                try:
                    self.usage["cost_usd"] = self.usage.get("cost_usd", 0.0) + float(usage["cost"])
                except (TypeError, ValueError):
                    self.usage["cost_usd"] = self.usage.get("cost_usd", 0.0)
        served = payload.get("model")
        if isinstance(served, str) and served:
            self.served_model = served

    def summary(self) -> str:
        """One line for the end of a run: what was asked, spent and refused."""
        parts = [
            f"{self.stats['requests']} request(s) for {self.stats['steps']} step(s)",
            f"{self.stats['retries']} retried",
        ]
        if self.stats["failures"]:
            parts.append(f"{self.stats['failures']} failed")
        if self.stats["fallbacks"]:
            parts.append(f"{self.stats['fallbacks']} on heuristic fallback")
        tokens = self.usage.get("total_tokens") or (
            self.usage.get("prompt_tokens", 0) + self.usage.get("completion_tokens", 0)
        )
        if tokens:
            parts.append(f"{int(tokens)} tokens")
        if "cost_usd" in self.usage:
            parts.append(f"${self.usage['cost_usd']:.6f}")
        if self.served_model and self.served_model != self.model:
            parts.append(f"served by {self.served_model}, not {self.model}")
        return ", ".join(parts)

    def _backoff(self, attempt: int, text: str) -> float:
        """Honour ``Retry-After`` when the endpoint bothered to send one.

        A 429 from a rate limiter usually comes with a number; ignoring it and
        guessing 0.5s/1s is how a free tier turns a rate limit into a permanent
        ban. Falls back to exponential backoff with a ceiling.
        """
        header = re.search(r'"retry[_-]?after"\s*:\s*"?(\d+)"?', text, re.IGNORECASE)
        if header:
            return min(20.0, float(header.group(1)))
        return min(4.0, 0.5 * (2 ** (attempt - 1)))

    def _error_message(self, status: int, text: str) -> str:
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            parsed = None
        detail = ""
        if isinstance(parsed, dict):
            error = parsed.get("error")
            if isinstance(error, dict):
                detail = str(error.get("message") or error.get("code") or "")[:200]
            elif error:
                detail = str(error)[:200]
            elif parsed.get("detail"):
                detail = str(parsed["detail"])[:200]
        if not detail:
            detail = re.sub(r"\s+", " ", text or "")[:160]
        hints = {
            401: "the key was rejected -- check it is the right one and is not expired",
            402: "this model is not on a free tier and the account has no credit",
            403: "the key is valid but not allowed for this model",
            404: f"no such model at this endpoint: {self.model!r} (set by {MODEL_ENV})",
            429: "rate limited by the provider; its retry budget is already spent, so wait or pick another model",
        }.get(status)
        return f"HTTP {status} from {detail or 'provider'}" + (f" -- {hints}" if hints else "")

    def _unwrap(self, text: str) -> dict[str, Any]:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise PolicyError(f"provider replied with non-JSON body: {text[:120]!r}") from None
        if not isinstance(payload, dict):
            raise PolicyError("provider reply is not an object")
        if payload.get("error"):
            raise PolicyError(f"provider error: {str(payload['error'])[:200]}")
        self._record_usage(payload)
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise PolicyError("provider returned no choices")
        first = choices[0] if isinstance(choices[0], dict) else {}
        if first.get("finish_reason") == "length":
            raise PolicyError(
                "the reply was cut off at max_tokens, so it cannot be parsed; raise max_tokens "
                "or shorten the screen (max_interactables)"
            )
        message = first.get("message") or {}
        content = message.get("content")
        if content is None:
            content = (first.get("text") or "")
        if not isinstance(content, str):
            # Some endpoints hand back a content *list* of parts.
            if isinstance(content, list):
                content = "".join(
                    str(part.get("text", "")) if isinstance(part, dict) else str(part) for part in content
                )
            else:
                raise PolicyError(f"unexpected message content type {type(content).__name__}")
        return extract_json(content)

    # ------------------------------------------------------------- mapping

    def _to_action(self, reply: dict[str, Any], observation: Observation) -> Action:
        raw_name = reply.get("action", reply.get("op", reply.get("name")))
        if raw_name is None:
            self._reject("the reply has no \"action\" key")
        name = str(raw_name or "").strip().lower().replace("-", "_").replace(" ", "_")
        # Models like to explain the namespace they think they are in.
        name = name.rsplit(".", 1)[-1]
        op = _ACTION_ALIASES.get(name)
        if op is None:
            self._reject(
                f"unknown action {name!r}; choose one of {', '.join(sorted(set(_ACTION_ALIASES)))}"
            )
        try:
            action = self._build(op, reply, observation)
        except PolicyError as exc:
            self._reject(str(exc))
            raise
        self._last_rejection = ""
        # The factories default to source="policy", which would make an LLM-driven
        # trace indistinguishable from the heuristic one. Provenance is the only
        # way a reader of trace.jsonl can tell which steps a model chose, and the
        # dry-run default is keyed to exactly that distinction. The terminal
        # factories already say something more specific than "who", so leave those.
        if action.source == "policy":
            action = replace(action, source=self.name)
        return action

    def _reject(self, reason: str) -> None:
        """Remember why this reply was unusable, for the *next* prompt.

        The engine treats three consecutive refusals as a policy bug and ends the
        run. Feeding the rejection back turns that into at most one wasted step,
        which is the difference between a run that completes and one that stops
        because a model put an index in quotes badly.
        """
        self._last_rejection = reason
        raise PolicyError(reason)

    def _build(self, op: Op, reply: dict[str, Any], observation: Observation) -> Action:
        rationale = str(reply.get("rationale") or reply.get("reason") or "").strip()[:240]
        args = reply.get("args") if isinstance(reply.get("args"), dict) else {}

        def arg(key: str, default: Any = None) -> Any:
            return reply[key] if key in reply else args.get(key, default)

        if op is Op.TAP_INDEX:
            index = _int(arg("index"), "index", reply)
            self._check_bounds(index, observation)
            return Action.tap(index, rationale)
        if op is Op.TAP_POINT:
            return Action.tap_point(
                _int(arg("x"), "x", reply), _int(arg("y"), "y", reply), rationale
            )
        if op is Op.LONG_PRESS_INDEX:
            index = _int(arg("index"), "index", reply)
            self._check_bounds(index, observation)
            duration = arg("duration_ms", 700)
            return Action.long_press(index, _int(duration, "duration_ms", reply), rationale)
        if op is Op.SCROLL:
            return Action.scroll(_str(arg("direction", "down"), "direction", reply), rationale)
        if op is Op.SWIPE:
            keys = ("x1", "y1", "x2", "y2")
            missing = [k for k in keys if arg(k) is None]
            if missing:
                raise PolicyError(f"swipe needs {', '.join(keys)}; missing {', '.join(missing)}")
            return Action.swipe(
                *(_int(arg(k), k, reply) for k in keys),
                duration_ms=_int(arg("duration_ms", 300), "duration_ms", reply),
                rationale=rationale,
            )
        if op is Op.TEXT:
            return Action.text(_str(arg("value", reply.get("text")), "value", reply), rationale)
        if op is Op.KEYEVENT:
            code = arg("code", arg("key"))
            if code is None:
                raise PolicyError("keyevent needs a code (66) or a name (KEYCODE_ENTER)")
            if isinstance(code, str) and not code.strip().isdigit():
                # Names are friendlier for a model to write; the device wants a
                # number. Strip the prefix as a prefix -- `lstrip("KEYCODE_")`
                # would eat the E of ENTER too, since lstrip takes a character set.
                name = code.strip().upper().removeprefix("KEYCODE_").removeprefix("KEY_")
                numeric = _KEYCODES.get(name)
                if numeric is None:
                    raise PolicyError(
                        f"unknown key name {code!r}; use a number, or one of: "
                        f"{', '.join(sorted(_KEYCODES))}"
                    )
                code = numeric
            return Action.keyevent(_int(code, "code", reply), rationale)
        if op is Op.NOOP:
            # A model that wants to think again is not an error; it is a step with
            # no side effect, and the livelock check is what keeps it honest.
            return Action.noop(rationale or "model requested no action")
        if op is Op.BACK:
            return Action.back(rationale)
        if op is Op.HOME:
            return Action.home(rationale)
        if op is Op.LAUNCH_APP:
            return Action.launch_app(_str(arg("package", arg("app")), "package", reply), rationale)
        if op is Op.START_ACTIVITY:
            return Action.start_activity(_str(arg("component"), "component", reply), rationale)
        if op is Op.OPEN_URL:
            return Action.open_url(_str(arg("url"), "url", reply), rationale)
        if op is Op.WAIT:
            value = arg("seconds", 1.0)
            try:
                seconds = float(value)
            except (TypeError, ValueError):
                raise PolicyError(f"wait needs a number of seconds, got {value!r}") from None
            return Action.wait(min(10.0, max(0.05, seconds)), rationale)
        if op is Op.DONE:
            return Action.done(rationale or "model reported the goal reached")
        if op is Op.ABORT:
            return Action.abort(rationale or "model aborted without giving a reason")
        if op is Op.RAW_SHELL:
            # Translated, not refused. Whether a model may compose a command is
            # the profile's decision, and the engine's gate already answers it;
            # blocking it a second time here would move safety into the policy,
            # which is the exact thing this project refuses to do.
            return Action.raw_shell(_str(arg("command"), "command", reply), rationale)
        raise PolicyError(f"action {op.value!r} cannot be produced from a model reply")

    @staticmethod
    def _check_bounds(index: int, observation: Observation) -> None:
        count = observation.extra.get("indexed_count")
        if not isinstance(count, int) or count <= 0:
            return
        if not 0 <= index < count:
            raise PolicyError(
                f"index {index} does not exist on this screen; the digest shows 0..{count - 1}"
            )

    # ---------------------------------------------------------- transport

    def _post(self, url: str, headers: dict[str, str], body: bytes, timeout: float) -> tuple[int, str]:
        """One HTTP call. Kept tiny and boring on purpose.

        ``HTTPError`` carries the provider's body, which is the only useful part
        of a 4xx, so it is read and returned as a status rather than raised --
        which is also what makes the retry loop above the only place that
        decides whether a failure is worth another call.
        """
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return int(response.status or 200), response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace") if exc.fp else ""
            return int(exc.code), raw
        except urllib.error.URLError as exc:
            raise PolicyError(f"cannot reach {url}: {exc.reason}") from None
        except TimeoutError:
            raise PolicyError(f"{url} did not answer within {timeout}s") from None


#: The handful of key names a model reliably produces, and nothing more: an
#: exhaustive table would be an Android SDK constant list nobody maintains, and
#: numbers are always accepted anyway.
_KEYCODES = {
    "ENTER": 66,
    "BACK": 4,
    "HOME": 3,
    "MENU": 82,
    "DELETE": 67,
    "DEL": 67,
    "SPACE": 62,
    "TAB": 61,
    "ESCAPE": 111,
    "ESC": 111,  # what models actually write
    "APP_SWITCH": 187,
    "VOLUME_UP": 24,
    "VOLUME_DOWN": 25,
    "POWER": 26,
    "DPAD_UP": 19,
    "DPAD_DOWN": 20,
    "DPAD_LEFT": 21,
    "DPAD_RIGHT": 22,
    "DPAD_CENTER": 23,
}

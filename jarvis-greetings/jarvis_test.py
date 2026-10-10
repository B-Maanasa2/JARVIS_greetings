"""JARVIS-Greetings V0.3.1: submit requests and read Agent Studio SSE results."""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests
from dotenv import load_dotenv


class AgentError(Exception):
    """A safe, user-facing client failure (never includes response bodies or keys)."""


def _name(value: str) -> str:
    return value.lower().replace("_", "").replace("-", "").replace(".", "")


# Semantic detection vocabulary, not a claim about this deployment's schema.
ENVELOPES = {"data", "result", "response", "output", "payload", "task", "transaction", "job"}
DONE = {"done", "complete", "completed", "finished", "end", "success", "succeeded", "taskcompleted", "workflowcompleted"}
FAILED = {"error", "failed", "failure", "cancelled", "canceled", "taskfailed"}
PROGRESS = {"progress", "metadata", "status", "heartbeat", "ping", "start", "started", "queued", "pending", "accepted", "submitted", "running", "inprogress", "processing", "tool", "toolcall", "toolresult", "reasoning", "thinking", "log"}
DELTAS = {"delta", "token", "chunk", "textdelta", "contentdelta", "messagedelta", "responsedelta"}
FINALS = {"final", "finalanswer", "answer", "result", "response", "greeting", "output", "messagecompleted", "responsecompleted"}


def _objects(value: object, depth: int = 0):
    """Inspect only response envelopes, never arbitrary tool/database records."""
    if depth > 12:
        raise AgentError("Agent response nesting exceeds the inspection limit.")
    if isinstance(value, dict):
        yield value
        for key, child in value.items():
            if _name(key) in ENVELOPES and isinstance(child, dict):
                yield from _objects(child, depth + 1)


def _agent_failed(value: object) -> bool:
    return any(
        _name(str(obj.get("status", ""))) in FAILED
        or _name(str(obj.get("type", obj.get("event", "")))) in FAILED
        or obj.get("success") is False
        or bool(obj.get("error"))
        for obj in _objects(value)
    )


def _transaction_id(value: dict) -> str | None:
    candidates = []
    for obj in _objects(value):
        for key, item in obj.items():
            if _name(key) in {"transactionid", "taskid", "jobid"}:
                if not isinstance(item, str) or not item.strip():
                    raise AgentError("Transaction/task ID field must contain a nonempty string.")
                candidates.append(item.strip())
        for key in ("task", "transaction", "job"):
            child = obj.get(key)
            if isinstance(child, dict) and "id" in child:
                item = child["id"]
                if not isinstance(item, str) or not item.strip():
                    raise AgentError("Transaction/task ID field must contain a nonempty string.")
                candidates.append(item.strip())
    unique = set(candidates)
    if len(unique) > 1:
        raise AgentError("Ambiguous response: multiple different transaction/task IDs were returned.")
    return next(iter(unique), None)


def _text_candidates(value: object, mode: str = "snapshot", depth: int = 0) -> list[tuple[str, str]]:
    if depth > 12:
        raise AgentError("Agent response nesting exceeds the inspection limit.")
    if isinstance(value, str):
        return [(value, mode)] if value.strip() else []
    if not isinstance(value, dict):
        return []
    role = _name(str(value.get("role", "")))
    if role and role not in {"assistant", "agent"}:
        return []
    status = _name(str(value.get("status", "")))
    if status in PROGRESS or _name(str(value.get("type", value.get("event", "")))) in PROGRESS:
        return []
    candidates = []
    for key, child in value.items():
        name = _name(key)
        if name in {"greeting", "answer", "finalanswer", "outputtext", "agentresponse"}:
            candidates.extend(_text_candidates(child, "final", depth + 1))
        elif name == "delta":
            candidates.extend(_text_candidates(child, "delta", depth + 1))
        elif name in {"text", "content", "message"}:
            # Completion notifications are not assistant greetings.
            if name == "message" and status in DONE and not role:
                continue
            if isinstance(child, list) and name == "content":
                parts = []
                for part in child:
                    if isinstance(part, dict) and _name(str(part.get("type", "text"))) in {"text", "outputtext"}:
                        parts.extend(_text_candidates(part, mode, depth + 1))
                if parts:
                    candidates.append(("".join(text for text, _ in parts), mode))
            else:
                candidates.extend(_text_candidates(child, mode, depth + 1))
        elif name in ENVELOPES:
            candidates.extend(_text_candidates(child, mode, depth + 1))
        elif name == "choices" and isinstance(child, list):
            # More than one alternative is deliberately reported as ambiguous.
            for choice in child:
                candidates.extend(_text_candidates(choice, mode, depth + 1))
        elif name == "messages" and isinstance(child, list):
            assistant_messages = [item for item in child if isinstance(item, dict) and _name(str(item.get("role", ""))) in {"assistant", "agent"}]
            if assistant_messages:
                candidates.extend(_text_candidates(assistant_messages[-1], mode, depth + 1))
    return candidates


class _Greeting:
    def __init__(self):
        self.delta = ""
        self.snapshot = ""
        self.final = ""
        self.completed = False
        self.ambiguous_snapshot = False

    def feed(self, event: str, value: object) -> None:
        event_name = _name(event)
        kind = _name(str(value.get("type", value.get("event", "")))) if isinstance(value, dict) else ""
        if event_name in FAILED or kind in FAILED or _agent_failed(value):
            raise AgentError("Agent reported an error in its response.")
        final_markers = {"final", "finalanswer", "messagecompleted", "responsecompleted"}
        completed = event_name in DONE | final_markers or kind in DONE | final_markers or any(
            _name(str(obj.get("status", ""))) in DONE or obj.get("done") is True
            or _name(str(obj.get("type", obj.get("event", "")))) in DONE | final_markers
            for obj in _objects(value)
        )
        if (event_name in PROGRESS or kind in PROGRESS) and not completed:
            return
        mode = "delta" if event_name in DELTAS or kind in DELTAS else (
            "final" if event_name in FINALS or kind in FINALS or completed else "snapshot"
        )
        notification = event_name in DONE or kind in DONE or any(
            _name(str(obj.get("status", ""))) in DONE or obj.get("done") is True
            for obj in _objects(value)
        )
        text_value = value
        if notification and isinstance(value, str):
            text_value = {}  # A plain completion notification is not a greeting.
        elif notification and isinstance(value, dict) and not value.get("role"):
            text_value = {
                key: child for key, child in value.items()
                if _name(key) != "message" or isinstance(child, dict)
            }
        candidates = _text_candidates(text_value, mode)
        texts = {text for text, _ in candidates}
        if len(texts) > 1:
            raise AgentError("Ambiguous response: multiple different greeting text fields were returned.")
        if candidates:
            text = candidates[0][0]
            modes = {candidate_mode for _, candidate_mode in candidates}
            if "delta" in modes and len(modes) > 1:
                raise AgentError("Ambiguous response: greeting text is marked as both a chunk and a full answer.")
            selected = "delta" if "delta" in modes else "final" if "final" in modes else "snapshot"
            if selected == "delta":
                self.delta += text
            elif selected == "final":
                self.final = text
            else:
                if self.snapshot and text != self.snapshot and not text.startswith(self.snapshot):
                    # Deep Reality emits unlabelled content fragments before a
                    # completed event containing the authoritative full result.
                    # Keep reading instead of failing before that result arrives.
                    self.ambiguous_snapshot = True
                self.snapshot = text
        self.completed = self.completed or completed

    def finish(self) -> str:
        if self.final:
            return self.final.strip()
        if self.ambiguous_snapshot:
            raise AgentError("Ambiguous stream text: unlabelled chunks cannot be distinguished from complete answers, and no final result was returned.")
        if self.delta:
            if not self.completed:
                raise AgentError("Streaming failure: token stream ended without a completion event.")
            if self.snapshot and self.snapshot != self.delta:
                raise AgentError("Ambiguous response: streamed chunks and full text disagree.")
            return self.delta.strip()
        if self.snapshot.strip():
            return self.snapshot.strip()
        raise AgentError("Missing or empty greeting text field: no identifiable agent answer was returned.")


@dataclass(frozen=True)
class Config:
    api_url: str
    stream_template: str
    api_key: str = field(repr=False)
    connect_timeout: float = 10
    read_timeout: float = 30
    deadline: float = 180

    @classmethod
    def from_env(cls) -> Config:
        load_dotenv(Path(__file__).with_name(".env"), override=False)

        def required(name: str) -> str:
            value = os.environ.get(name, "").strip()
            if not value:
                raise AgentError(f"Set {name} in the local .env file before contacting JARVIS.")
            return value

        def seconds(name: str, default: str) -> float:
            try:
                value = float(os.environ.get(name, default))
                if not math.isfinite(value) or value <= 0:
                    raise ValueError
                return value
            except ValueError:
                raise AgentError(f"{name} must be a positive finite number.") from None

        config = cls(
            api_url=required("AGENT_API_URL"),
            stream_template=required("AGENT_STREAM_URL_TEMPLATE"),
            api_key=required("AGENT_API_KEY"),
            connect_timeout=seconds("AGENT_CONNECT_TIMEOUT", "10"),
            read_timeout=seconds("AGENT_READ_TIMEOUT", "30"),
            deadline=seconds("AGENT_STREAM_DEADLINE", "180"),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if self.stream_template.count("{transaction_id}") != 1:
            raise AgentError("Stream URL template must contain {transaction_id} exactly once.")
        for url in (self.api_url, self.stream_template):
            try:
                parsed = urlsplit(url)
                parsed.port
            except ValueError:
                raise AgentError("API or stream endpoint URL is invalid.") from None
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise AgentError("API and stream URLs must use HTTPS without embedded credentials.")
        if urlsplit(self.api_url).netloc != urlsplit(self.stream_template).netloc:
            raise AgentError("API and stream URLs must use the same host to protect the Bearer key.")
        if "\n" in self.api_key or "\r" in self.api_key:
            raise AgentError("API key must be a single line.")


def _check_http(response: requests.Response) -> None:
    if response.status_code in {401, 403}:
        raise AgentError("Authentication failed. Check the local API key and deployment access.")
    if response.status_code == 404:
        raise AgentError("Agent API returned HTTP 404. Check the endpoint URL and deployment availability.")
    if not 200 <= response.status_code < 300:
        raise AgentError(f"Agent API returned HTTP {response.status_code}.")


def _stream_lines(response: requests.Response, deadline: float):
    """Bound even a stream that keeps sending bytes without a newline."""
    started = time.monotonic()
    buffer = bytearray()
    size = 0
    after_cr = False
    first = True
    for chunk in response.iter_content(chunk_size=1):
        if time.monotonic() - started > deadline:
            raise AgentError("Agent stream exceeded the configured deadline.")
        size += len(chunk)
        if size > 2_000_000:
            raise AgentError("Agent stream exceeded the 2 MB response limit.")
        for byte in chunk:
            if byte == 10 and after_cr:
                after_cr = False
                continue
            after_cr = byte == 13
            if byte in {10, 13}:
                line = buffer.decode("utf-8")
                if first:
                    line = line.removeprefix("\ufeff")
                    first = False
                yield line
                buffer.clear()
            else:
                buffer.append(byte)
    if buffer:
        yield buffer.decode("utf-8").removeprefix("\ufeff") if first else buffer.decode("utf-8")


def _sse_events(response: requests.Response, deadline: float):
    """Parse SSE frames, including multiline data and final unterminated frames."""
    event = "message"
    lines: list[str] = []
    for line in _stream_lines(response, deadline):
        if not line:
            if lines or event != "message":
                yield event, "\n".join(lines)
            event, lines = "message", []
        elif not line.startswith(":"):
            field, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if field == "event":
                event = value or "message"
            elif field == "data":
                lines.append(value)
    if lines or event != "message":
        yield event, "\n".join(lines)


def _decode_event(data: str) -> object:
    try:
        return json.loads(data)
    except ValueError:
        if data.lstrip().startswith(("{", "[", '"')):
            raise AgentError("Agent stream contains malformed JSON.") from None
        return data


def _safe_text(text: str, config: Config) -> str:
    # Also redact the key if the server accidentally echoes it in greeting text.
    text = text.replace(config.api_key, "[REDACTED]")
    if "authorization:" in text.lower() or "bearer " in text.lower():
        raise AgentError("Agent text contains authentication details and cannot be displayed safely.")
    return text


def _call(request_text: str, config: Config) -> dict:
    headers = {"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json"}
    timeout = (config.connect_timeout, config.read_timeout)
    with requests.post(
        config.api_url, headers=headers, json={"query": request_text},
        timeout=timeout, allow_redirects=False,
    ) as response:
        _check_http(response)
        try:
            submission = response.json()
        except ValueError:
            raise AgentError("Submission response is malformed JSON.") from None
    if not isinstance(submission, dict):
        raise AgentError("Submission response must be a JSON object.")
    if _agent_failed(submission):
        raise AgentError("Agent reported an error in its submission response.")
    transaction_id = _transaction_id(submission)
    if transaction_id is None:
        direct = _Greeting()
        direct.feed("message", submission)
        try:
            greeting = direct.finish()
        except AgentError:
            raise AgentError("Missing transaction/task ID field and no unambiguous final greeting in the POST response.") from None
        return {"submission": submission, "events": [], "greeting": _safe_text(greeting, config)}
    stream_url = config.stream_template.replace("{transaction_id}", quote(transaction_id, safe=""))
    events = []
    greeting = _Greeting()
    with requests.get(
        stream_url, headers={"Authorization": headers["Authorization"], "Accept": "text/event-stream"},
        stream=True, timeout=timeout, allow_redirects=False,
    ) as response:
        _check_http(response)
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type == "text/event-stream":
            for event, data in _sse_events(response, config.deadline):
                if data.strip() == "[DONE]":
                    greeting.completed = True
                    break
                # Error events are handled before interpreting their data.
                if _name(event) in FAILED:
                    raise AgentError("Agent reported an error in its response stream.")
                parsed = _decode_event(data) if data.strip() else {}
                events.append({"event": event, "data": parsed})
                greeting.feed(event, parsed)
                if greeting.completed:
                    break
        elif content_type == "application/json" or content_type.endswith("+json"):
            # Some deployments return a complete JSON result at the task URL.
            try:
                body = "\n".join(_stream_lines(response, config.deadline))
                parsed = json.loads(body)
            except ValueError:
                raise AgentError("Task result contains malformed JSON.") from None
            events.append({"event": "message", "data": parsed})
            greeting.feed("message", parsed)
        else:
            raise AgentError("Streaming failure: expected text/event-stream or a JSON task result.")
    return {"submission": submission, "events": events, "greeting": _safe_text(greeting.finish(), config)}


def call_agent_api(request_text: str) -> dict:
    """Inspect actual API responses and return the parsed result plus agent text."""
    if not isinstance(request_text, str) or not request_text.strip():
        raise AgentError("Request text cannot be empty.")
    config = Config.from_env()
    try:
        return _call(request_text.strip(), config)
    except requests.Timeout:
        raise AgentError("Agent request timed out. You can try another request.") from None
    except requests.ConnectionError:
        raise AgentError("Could not connect to the agent or the result stream was interrupted.") from None
    except requests.RequestException:
        raise AgentError("Agent HTTP request failed.") from None
    except UnicodeError:
        raise AgentError("Agent stream contains invalid UTF-8.") from None
    except (RecursionError, ValueError):
        raise AgentError("Agent response or endpoint could not be interpreted safely.") from None


def extract_greeting(response: dict) -> str:
    """Read the client's normalized greeting, not an assumed remote JSON path."""
    greeting = response.get("greeting") if isinstance(response, dict) else None
    if not isinstance(greeting, str) or not greeting.strip():
        raise AgentError("Missing or empty greeting text in the parsed agent response.")
    return greeting.strip()


def main() -> int:
    print("\n🤖 JARVIS GREETINGS — V0.3.1\n" + "-" * 32)
    try:
        # Only endpoints, key, and timeouts are needed; responses are inspected automatically.
        Config.from_env()
    except AgentError as error:
        print(f"Configuration error: {error}")
        return 1
    while True:
        try:
            name = input("\nEnter your name (or 'exit'): ").strip()
            if name.lower() == "exit":
                break
            if not name:
                print("Please enter a name, or type exit.")
                continue
            print("\nConnecting to JARVIS...\n")
            try:
                print(f"JARVIS: {extract_greeting(call_agent_api(f'Greet {name}'))}")
            except AgentError as error:
                print(f"Error: {error}")
            print("\n" + "-" * 32)
        except (EOFError, KeyboardInterrupt):
            break
    print("\nSession ended.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

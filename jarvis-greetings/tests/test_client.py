"""Offline tests. All response fields and greetings below are fictional fixtures."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import jarvis_test as client


@pytest.fixture(autouse=True)
def environment(monkeypatch):
    # Never read a real key or call a real API during tests.
    monkeypatch.setattr(client, "load_dotenv", lambda *args, **kwargs: None)
    settings = {
        "AGENT_API_URL": "https://example.invalid/deployments/test/ask",
        "AGENT_STREAM_URL_TEMPLATE": "https://example.invalid/task/{transaction_id}/stream",
        "AGENT_API_KEY": "fictional-test-key",
        "AGENT_CONNECT_TIMEOUT": "10",
        "AGENT_READ_TIMEOUT": "30",
        "AGENT_STREAM_DEADLINE": "180",
    }
    for key, value in settings.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(client.requests, "post", MagicMock(side_effect=AssertionError("Unexpected network request")))
    monkeypatch.setattr(client.requests, "get", MagicMock(side_effect=AssertionError("Unexpected network request")))


def response(status=200, payload=None, lines=None, content_type="text/event-stream"):
    result = MagicMock()
    result.__enter__.return_value = result
    result.status_code = status
    result.json.return_value = payload if payload is not None else {"job": {"id": "test-job"}}
    result.headers = {"Content-Type": content_type}
    stream_lines = lines if lines is not None else [
        "event: answer", 'data: {"result": {"text": "Fixture greeting"}}', "",
        "event: finished", "data: {}", "",
    ]
    result.iter_content.return_value = iter([("\n".join(stream_lines) + "\n").encode("utf-8")])
    return result


def setup_api(monkeypatch, post_response=None, stream_response=None):
    post = MagicMock(return_value=post_response or response())
    get = MagicMock(return_value=stream_response or response())
    monkeypatch.setattr(client.requests, "post", post)
    monkeypatch.setattr(client.requests, "get", get)
    return post, get


def test_successful_greeting(monkeypatch):
    post, get = setup_api(monkeypatch)
    result = client.call_agent_api("Greet Albin")
    assert client.extract_greeting(result) == "Fixture greeting"
    assert result["submission"] == {"job": {"id": "test-job"}}
    assert post.call_args.kwargs["json"] == {"query": "Greet Albin"}
    assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer fictional-test-key"
    assert post.call_args.kwargs["timeout"] == (10, 30)
    assert post.call_args.kwargs["allow_redirects"] is False
    assert get.call_args.args[0] == "https://example.invalid/task/test-job/stream"
    assert get.call_args.kwargs["stream"] is True


def test_unknown_user_preserves_agent_text(monkeypatch):
    stream = response(lines=["event: answer", 'data: {"result":{"text":"Unknown user fixture"}}', "", "event: finished", ""])
    setup_api(monkeypatch, stream_response=stream)
    assert client.extract_greeting(client.call_agent_api("Greet Unknown")) == "Unknown user fixture"


@pytest.mark.parametrize("text", ["", "  ", None])
def test_empty_input_does_not_contact_api(text):
    with pytest.raises(client.AgentError, match="cannot be empty"):
        client.call_agent_api(text)
    client.requests.post.assert_not_called()


@pytest.mark.parametrize("status, message", [(401, "Authentication failed"), (403, "Authentication failed"), (500, "HTTP 500"), (302, "HTTP 302")])
@pytest.mark.parametrize("stage", ["post", "stream"])
def test_http_failures(monkeypatch, status, message, stage):
    post, get = setup_api(monkeypatch,
        post_response=response(status=status) if stage == "post" else None,
        stream_response=response(status=status) if stage == "stream" else None)
    with pytest.raises(client.AgentError, match=message):
        client.call_agent_api("Greet Albin")
    if stage == "post":
        get.assert_not_called()


@pytest.mark.parametrize("stage", ["post", "stream"])
@pytest.mark.parametrize("error, message", [(requests.Timeout, "timed out"), (requests.ConnectionError, "Could not connect"), (requests.RequestException, "HTTP request failed")])
def test_transport_errors_are_sanitized(monkeypatch, stage, error, message):
    post, get = setup_api(monkeypatch)
    (post if stage == "post" else get).side_effect = error("fictional-test-key and private server details")
    with pytest.raises(client.AgentError, match=message) as caught:
        client.call_agent_api("Greet Albin")
    assert "fictional-test-key" not in str(caught.value)
    assert "private" not in str(caught.value)


def test_malformed_submission_json(monkeypatch):
    bad = response()
    bad.json.side_effect = ValueError("private payload")
    setup_api(monkeypatch, post_response=bad)
    with pytest.raises(client.AgentError, match="malformed JSON"):
        client.call_agent_api("Greet Albin")


def test_malformed_stream_json(monkeypatch):
    setup_api(monkeypatch, stream_response=response(lines=["event: answer", 'data: {"broken":', ""]))
    with pytest.raises(client.AgentError, match="malformed JSON"):
        client.call_agent_api("Greet Albin")


def test_missing_greeting_field(monkeypatch):
    setup_api(monkeypatch, stream_response=response(lines=["event: answer", "data: {}", "", "event: finished", ""]))
    with pytest.raises(client.AgentError, match="Missing or empty greeting"):
        client.extract_greeting(client.call_agent_api("Greet Albin"))


@pytest.mark.parametrize("payload", [{}, {"job": {"id": ""}}, {"job": {"id": 12}}, []])
def test_invalid_transaction_response(monkeypatch, payload):
    _, get = setup_api(monkeypatch, post_response=response(payload=payload))
    with pytest.raises(client.AgentError):
        client.call_agent_api("Greet Albin")
    get.assert_not_called()


def test_multiple_sequential_requests(monkeypatch):
    post, get = setup_api(monkeypatch)
    # Each call receives a fresh stream iterator.
    get.side_effect = [response(), response()]
    for name in ["Albin", "Maanasa"]:
        assert client.extract_greeting(client.call_agent_api(f"Greet {name}")) == "Fixture greeting"
    assert [call.kwargs["json"] for call in post.call_args_list] == [{"query": "Greet Albin"}, {"query": "Greet Maanasa"}]


def test_interactive_repeated_names_empty_input_and_exit(monkeypatch, capsys):
    post, get = setup_api(monkeypatch)
    get.side_effect = [response(), response()]
    inputs = iter(["  ", "Albin", "Maanasa", " EXIT "])
    monkeypatch.setattr("builtins.input", lambda prompt: next(inputs))
    assert client.main() == 0
    output = capsys.readouterr().out
    assert output.count("JARVIS: Fixture greeting") == 2
    assert "Please enter a name" in output
    assert "Session ended." in output
    assert post.call_count == 2
    assert "fictional-test-key" not in output


@pytest.mark.parametrize("error", [EOFError, KeyboardInterrupt])
def test_graceful_terminal_exit(monkeypatch, capsys, error):
    def interrupted(prompt):
        raise error
    monkeypatch.setattr("builtins.input", interrupted)
    assert client.main() == 0
    assert "Session ended." in capsys.readouterr().out
    client.requests.post.assert_not_called()


def test_cli_recovers_after_error(monkeypatch, capsys):
    post, get = setup_api(monkeypatch)
    post.side_effect = [requests.Timeout(), response()]
    inputs = iter(["Albin", "Maanasa", "exit"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(inputs))
    assert client.main() == 0
    output = capsys.readouterr().out
    assert "timed out" in output and "JARVIS: Fixture greeting" in output


def test_delta_chunks_and_multiline_sse(monkeypatch):
    stream = response(lines=[": keepalive", "event: metadata", "data: {}", "", "event: delta",
        'data: {"result":', 'data: {"text":"Hello "}}', "", "event: delta",
        'data: {"result":{"text":"fixture"}}', "", "event: finished", ""])
    setup_api(monkeypatch, stream_response=stream)
    assert client.extract_greeting(client.call_agent_api("Future natural-language event")) == "Hello fixture"


def test_plain_text_and_literal_completion_marker(monkeypatch):
    setup_api(monkeypatch, stream_response=response(lines=["data: Fixture text", "", "data: [DONE]", ""]))
    assert client.extract_greeting(client.call_agent_api("Greet Albin")) == "Fixture text"


def test_missing_completion_marker(monkeypatch):
    setup_api(monkeypatch, stream_response=response(lines=["event: delta", 'data: {"text":"partial"}', ""]))
    with pytest.raises(client.AgentError, match="without a completion"):
        client.call_agent_api("Greet Albin")


def test_stream_error_event(monkeypatch):
    setup_api(monkeypatch, stream_response=response(lines=["event: failed", "data: private-details", ""]))
    with pytest.raises(client.AgentError, match="reported an error"):
        client.call_agent_api("Greet Albin")


def test_wrong_stream_content_type(monkeypatch):
    setup_api(monkeypatch, stream_response=response(content_type="text/html"))
    with pytest.raises(client.AgentError, match="text/event-stream"):
        client.call_agent_api("Greet Albin")


def test_stream_deadline(monkeypatch):
    setup_api(monkeypatch)
    times = iter([0, 181])
    monkeypatch.setattr(client.time, "monotonic", lambda: next(times))
    with pytest.raises(client.AgentError, match="deadline"):
        client.call_agent_api("Greet Albin")


def test_stream_size_limit(monkeypatch):
    setup_api(monkeypatch, stream_response=response(lines=["data: " + "x" * 2_000_001]))
    with pytest.raises(client.AgentError, match="2 MB"):
        client.call_agent_api("Greet Albin")


@pytest.mark.parametrize("key,value", [
    ("AGENT_API_KEY", ""), ("AGENT_API_URL", ""),
    ("AGENT_READ_TIMEOUT", "nan"), ("AGENT_READ_TIMEOUT", "0"), ("AGENT_CONNECT_TIMEOUT", "invalid"),
    ("AGENT_STREAM_URL_TEMPLATE", "https://other.invalid/task/{transaction_id}/stream"),
    ("AGENT_API_URL", "http://example.invalid/ask"), ("AGENT_API_KEY", "bad\nkey"),
])
def test_invalid_configuration_prevents_requests(monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(client.AgentError):
        client.call_agent_api("Greet Albin")
    client.requests.post.assert_not_called()


def test_no_configuration_cli_exit(monkeypatch, capsys):
    monkeypatch.setenv("AGENT_API_KEY", "")
    assert client.main() == 1
    assert "Configuration error:" in capsys.readouterr().out


def test_transaction_id_url_encoding(monkeypatch):
    _, get = setup_api(monkeypatch, post_response=response(payload={"job": {"id": "a/b?c"}}))
    client.call_agent_api("Greet Albin")
    assert get.call_args.args[0].endswith("a%2Fb%3Fc/stream")


@pytest.mark.parametrize("text", ["", 42, None])
def test_empty_or_nontext_greeting(text):
    with pytest.raises(client.AgentError):
        client.extract_greeting({"greeting": text})


def test_assistant_message_history(monkeypatch):
    payload = {"messages": [{"role": "user", "content": "Greet Albin"}, {"role": "assistant", "content": "Old greeting"}, {"role": "assistant", "content": "Fixture greeting"}]}
    setup_api(monkeypatch, post_response=response(payload=payload))
    assert client.extract_greeting(client.call_agent_api("Greet Albin")) == "Fixture greeting"


@pytest.mark.parametrize("payload", [
    {"transaction_id": "test-job"}, {"transactionId": "test-job"},
    {"data": {"task_id": "test-job"}}, {"transaction": {"id": "test-job"}},
    {"data": {"result": {"jobId": "test-job"}}},
])
def test_automatic_transaction_detection(monkeypatch, payload):
    _, get = setup_api(monkeypatch, post_response=response(payload=payload))
    assert client.extract_greeting(client.call_agent_api("Greet Albin")) == "Fixture greeting"
    assert get.call_args.args[0].endswith("test-job/stream")


def test_ambiguous_transaction_ids(monkeypatch):
    _, get = setup_api(monkeypatch, post_response=response(payload={"transaction_id": "one", "task_id": "two"}))
    with pytest.raises(client.AgentError, match="multiple different transaction"):
        client.call_agent_api("Greet Albin")
    get.assert_not_called()


@pytest.mark.parametrize("payload", [
    {"greeting": "Fixture greeting"}, {"answer": "Fixture greeting"},
    {"response": "Fixture greeting"}, {"data": {"output": {"text": "Fixture greeting"}}},
    {"choices": [{"message": {"role": "assistant", "content": "Fixture greeting"}}]},
    {"status": "completed", "result": "Fixture greeting"},
    {"message": {"role": "assistant", "content": [{"type": "text", "text": "Fixture "}, {"type": "text", "text": "greeting"}]}},
])
def test_direct_json_greetings(monkeypatch, payload):
    _, get = setup_api(monkeypatch, post_response=response(payload=payload))
    assert client.extract_greeting(client.call_agent_api("Greet Albin")) == "Fixture greeting"
    get.assert_not_called()


@pytest.mark.parametrize("payload", [
    {"answer": "one", "response": "two"},
    {"choices": [{"text": "one"}, {"text": "two"}]},
])
def test_ambiguous_text_is_not_selected(monkeypatch, payload):
    setup_api(monkeypatch, post_response=response(payload=payload))
    with pytest.raises(client.AgentError, match="multiple different greeting"):
        client.call_agent_api("Greet Albin")


@pytest.mark.parametrize("payload", [
    {"status": "pending", "message": "Accepted"},
    {"id": "could-be-an-agent-id"},
    {"query": "Greet Albin"}, {"profile": {"text": "Personality text"}},
    {"message": {"role": "user", "content": "Greet Albin"}},
])
def test_no_greeting_from_acknowledgement_or_user_data(monkeypatch, payload):
    _, get = setup_api(monkeypatch, post_response=response(payload=payload))
    with pytest.raises(client.AgentError, match="Missing transaction/task ID"):
        client.call_agent_api("Greet Albin")
    get.assert_not_called()


def test_json_task_result(monkeypatch):
    setup_api(monkeypatch, stream_response=response(content_type="application/json", lines=['{"result":{"text":"Fixture greeting"}}']))
    assert client.extract_greeting(client.call_agent_api("Greet Albin")) == "Fixture greeting"


def test_final_event_stops_without_waiting_for_connection_close(monkeypatch):
    stream = response()
    def chunks():
        yield b'event: final\ndata: {"response":"Fixture greeting"}\n\n'
        raise AssertionError("Should not read beyond explicit final event")
    stream.iter_content.return_value = chunks()
    setup_api(monkeypatch, stream_response=stream)
    assert client.extract_greeting(client.call_agent_api("Greet Albin")) == "Fixture greeting"
    stream.__exit__.assert_called_once()


def test_json_delta_type_and_completion_payload(monkeypatch):
    stream = response(lines=['data: {"type":"token","content":"Hello "}', "",
        'data: {"delta":{"content":"fixture"}}', "",
        'data: {"status":"completed","result":{"text":"Hello fixture"},"message":"Task done"}', ""])
    setup_api(monkeypatch, stream_response=stream)
    assert client.extract_greeting(client.call_agent_api("Greet Albin")) == "Hello fixture"


def test_cumulative_snapshots(monkeypatch):
    stream = response(lines=['data: {"text":"Hello"}', "", 'data: {"text":"Hello fixture"}', "", "data: [DONE]", ""])
    setup_api(monkeypatch, stream_response=stream)
    assert client.extract_greeting(client.call_agent_api("Greet Albin")) == "Hello fixture"


def test_unlabelled_disjoint_chunks_are_ambiguous(monkeypatch):
    stream = response(lines=['data: {"text":"Hello "}', "", 'data: {"text":"fixture"}', "", "data: [DONE]", ""])
    setup_api(monkeypatch, stream_response=stream)
    with pytest.raises(client.AgentError, match="unlabelled chunks"):
        client.call_agent_api("Greet Albin")


def test_completion_notification_is_not_greeting(monkeypatch):
    setup_api(monkeypatch, stream_response=response(lines=["event: completed", 'data: {"message":"Task completed"}', ""]))
    with pytest.raises(client.AgentError, match="Missing or empty greeting"):
        client.call_agent_api("Greet Albin")


@pytest.mark.parametrize("payload", [{"error": "private details"}, {"data": {"status": "failed"}}, {"data": {"type": "error"}}])
def test_agent_side_json_errors(monkeypatch, payload):
    setup_api(monkeypatch, post_response=response(payload=payload))
    with pytest.raises(client.AgentError, match="Agent reported an error") as caught:
        client.call_agent_api("Greet Albin")
    assert "private details" not in str(caught.value)


def test_key_echo_is_redacted(monkeypatch, capsys):
    setup_api(monkeypatch, post_response=response(payload={"answer": "Greeting fictional-test-key"}))
    inputs = iter(["Albin", "exit"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(inputs))
    assert client.main() == 0
    output = capsys.readouterr().out
    assert "fictional-test-key" not in output and "[REDACTED]" in output
    assert "fictional-test-key" not in repr(client.Config.from_env())


def test_authorization_echo_is_not_printed(monkeypatch):
    setup_api(monkeypatch, post_response=response(payload={"answer": "Authorization: Bearer private"}))
    with pytest.raises(client.AgentError, match="authentication details"):
        client.call_agent_api("Greet Albin")


def test_sse_crlf_unicode_and_bom(monkeypatch):
    stream = response()
    raw = '\ufeffevent: final\r\ndata: {"text":"Hello 🤖"}\r\n\r\n'.encode()
    stream.iter_content.return_value = iter(bytes([byte]) for byte in raw)
    setup_api(monkeypatch, stream_response=stream)
    assert client.extract_greeting(client.call_agent_api("Greet Albin")) == "Hello 🤖"


def test_deadline_without_line_breaks(monkeypatch):
    stream = response()
    stream.iter_content.return_value = iter([b"a", b"b"])
    times = iter([0, 1, 181])
    monkeypatch.setattr(client.time, "monotonic", lambda: next(times))
    setup_api(monkeypatch, stream_response=stream)
    with pytest.raises(client.AgentError, match="deadline"):
        client.call_agent_api("Greet Albin")


def test_interrupted_stream_does_not_return_partial_greeting(monkeypatch):
    stream = response()
    def chunks():
        yield b'event: delta\ndata: {"text":"partial"}\n\n'
        raise requests.ConnectionError("private-details")
    stream.iter_content.return_value = chunks()
    setup_api(monkeypatch, stream_response=stream)
    with pytest.raises(client.AgentError, match="interrupted"):
        client.call_agent_api("Greet Albin")


def test_only_six_environment_variables_are_required():
    assert client.Config.from_env().api_key == "fictional-test-key"


def test_plain_text_explicit_final_event(monkeypatch):
    setup_api(monkeypatch, stream_response=response(lines=["event: final", "data: Fixture greeting", ""]))
    assert client.extract_greeting(client.call_agent_api("Greet Albin")) == "Fixture greeting"


def test_dotenv_loaded_relative_to_script(monkeypatch):
    calls = []
    monkeypatch.setattr(client, "load_dotenv", lambda *args, **kwargs: calls.append((args, kwargs)))
    client.Config.from_env()
    assert calls[0] == ((Path(client.__file__).with_name(".env"),), {"override": False})


def test_deep_reality_content_chunks_then_completed_result(monkeypatch):
    # Field structure verified against the live deployment; text is fictional.
    stream = response(lines=[
        'data: {"status":"running"}', "",
        'data: {"status":"tool_start","tool":"fixture-tool"}', "",
        'data: {"status":"tool_end","tool":"fixture-tool"}', "",
        'data: {"content":"Hello"}', "",
        'data: {"content":" "}', "",
        'data: {"content":"Maanasa"}', "",
        'data: {"content":"!"}', "",
        'data: {"status":"completed","result":"Hello Maanasa!","execution_trace":[{"output_text_preview":"Internal workflow text"}]}', "",
    ])
    setup_api(monkeypatch, post_response=response(payload={"transaction_id":"test-job","stream_available":True}), stream_response=stream)
    assert client.extract_greeting(client.call_agent_api("Greet Maanasa")) == "Hello Maanasa!"


def test_ambiguous_chunks_continue_until_a_final_answer():
    greeting = client._Greeting()
    greeting.feed("message", {"content": "First "})
    greeting.feed("message", {"content": "second"})
    assert not greeting.completed
    greeting.feed("message", {"status": "completed", "result": "Actual final greeting"})
    assert greeting.finish() == "Actual final greeting"


def test_completed_without_final_text_does_not_guess_fragments():
    greeting = client._Greeting()
    greeting.feed("message", {"content": "First "})
    greeting.feed("message", {"content": "second"})
    greeting.feed("message", {"status": "completed"})
    with pytest.raises(client.AgentError, match="no final result"):
        greeting.finish()

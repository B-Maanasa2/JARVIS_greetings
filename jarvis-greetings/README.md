# JARVIS Greetings API Test Client — V0.3.1

The existing Python 3.10+ CLI calls the deployed JARVIS-Greetings agent. It never
retrieves personalities itself or generates a local greeting.

## Run

Enter your API key in `jarvis-greetings/.env`. The existing URLs and timeout
values are already populated; no response-schema settings are required.
The file is Git-ignored and has owner-only permissions. Environment variables
take precedence over the file, which is loaded relative to the script.

From the `JARVIS_TEST` workspace, run this single command:

```bash
.venv/bin/python jarvis-greetings/jarvis_test.py
```

Enter a name, then enter another name or `exit`. Ctrl+C and EOF also end the
session. A failed request displays a safe error and leaves the CLI ready for
another name. No failed request is automatically retried.

Dependencies are `requests`, `python-dotenv`, and `pytest` (testing). They are
installed in the workspace virtual environment. For a fresh checkout:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r jarvis-greetings/requirements.txt
```

Copy `.env.example` to `.env` if the local file is absent.

## Request and response

The CLI sends a Bearer-authenticated POST to `AGENT_API_URL` with the JSON body
`{"query": "Greet <entered name>"}`. It inspects the actual POST response for
transaction/task IDs. If a transaction is returned, it requests
`AGENT_STREAM_URL_TEMPLATE`, replacing `{transaction_id}` with the URL-encoded
ID. The stream endpoint can return SSE or a complete JSON result. A direct
unambiguous greeting in the POST JSON is also supported without streaming.

Response detection is automatic and conservative. Recognized ID candidates
include snake-case or camel-case transaction/task/job ID fields and `id`
inside a task, transaction, or job object. A generic top-level `id` is not
assumed to be a transaction ID. Text inspection recognizes greeting/answer
fields, text/content/message fields, response envelopes, assistant messages,
and a single choice. It excludes user/tool messages, personality records,
progress, metadata, and reasoning events. These are detection rules, not a
claim that any particular field is present in this deployment.

SSE parsing handles multiline data, UTF-8, comments, CR/LF line endings, JSON
and plain-text data, delta/token/chunk labels, explicit final events, completed
statuses, and `[DONE]`. Explicit chunks are concatenated; a final answer takes
precedence. Unlabelled snapshots must agree or grow by prefix. Disjoint unlabelled fragments are held until a final answer arrives. Without
a final answer, conflicting ID candidates, or multiple differing answer
fields, the client reports an ambiguity error instead of a silent choice. A token
stream that closes before completion is an error. A complete answer can be
returned when the stream closes normally. Completion notifications alone do
not count as greetings.

`call_agent_api(request_text)` remains reusable for arbitrary request text.
Its normalized result contains the parsed `submission`, `events`, and extracted
`greeting`. The normalized greeting key belongs to this client; it does not
assume that the remote API uses a field named `greeting`.
`extract_greeting(result)` returns the actual agent text.

Unsupported or ambiguous schemas produce a clear error without printing raw
responses, credentials, or Authorization headers. No manual JSON paths or
cURL are needed for the supported formats; an unfamiliar protocol may require
a code adaptation. HTTP authentication errors, missing/invalid endpoints,
connection failures, timeouts, malformed JSON, missing IDs/text, agent-side
error events, and interrupted streams are handled. The key is also redacted
if accidentally echoed in returned text.

Connections default to 10 seconds, idle reads to 30 seconds, and streams to a
180-second deadline checked as bytes arrive. An idle read can extend the
deadline by at most its read timeout. Stream responses are capped at 2 MB.
HTTPS, matching API/stream hosts, and disabled redirects protect the key.

## Tests and verification

```bash
.venv/bin/python -m pytest jarvis-greetings/tests -q
```

All fixtures are fictional and all network methods are mocked. Tests cover
both request stages, automatic schema detection, direct JSON, SSE formats,
ambiguity, error handling, secret redaction, repeated names, and graceful exit.

An approved live inspection confirmed that this deployment returns a
`transaction_id` and streams unlabelled `content` fragments followed by
`{"status":"completed","result":"<full greeting>"}`. The client waits for
that authoritative final result instead of rejecting the early fragments.
Workflow trace previews are excluded from greeting extraction. Mocked tests
use fictional text and do not independently verify personality lookup.

After the fix, an approved live CLI test for Maanasa displayed the actual
agent greeting and exited normally. The offline regression suite passed
92 tests.

# Hercules Runner

The existing Execute dialog supplies the **target system's** URL, username and
password. Platform login credentials are separate and are not copied into these
fields. Passwords are masked and cleared when the dialog closes.

API cases use `HerculesTestRunner` when `TEST_RUNNER_PROVIDER=hercules` (the local
default). Set `TEST_RUNNER_PROVIDER=central` and restart the backend to use the
existing deterministic executor. UI cases continue using Midscene.

## Start

Python 3.10+ is required. The runner has its own dependencies; do not install them
into or modify the Hercules virtual environment:

```bash
git clone https://github.com/zys-6/hercules-runner.git
cd hercules-runner
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
RUNNER_PYTHON="$PWD/.venv/bin/python" ./start.sh
```

`start.sh` creates an ignored, mode-0600 `data/service.token` if necessary. For a
backend started separately, load the same token without printing it:

```bash
export HERCULES_RUNNER_TOKEN="$(cat "$HOME/projects/hercules-runner/data/service.token")"
export TEST_RUNNER_PROVIDER=hercules
# Start/restart the platform backend with these environment variables.
```

Start this service independently of the platform. Configure the platform backend
with `HERCULES_RUNNER_URL=http://127.0.0.1:8096` and the same shared token.
Legacy platform startup scripts may still reference an in-repository runner;
update those paths before using them. Restart the backend after changing its
configuration. Do not overwrite a JAR while a JVM is running from it.

Defaults:

| Setting | Default |
| --- | --- |
| Runner listen address | `127.0.0.1:8096` |
| `HERCULES_HOME` | `~/projects/hercules` |
| `HERCULES_MODEL_PROFILE` | `ollama` |
| Model configuration | `$HERCULES_HOME/agents_llm_config.json` |
| `RUNNER_DATA_DIR` | `hercules-runner/data` |
| `HERCULES_RUNNER_URL` (backend) | `http://127.0.0.1:8096` |
| `HERCULES_TIMEOUT_SECONDS` (backend) | 600, bounded to 10–1800 |

Use one runner process/worker. A file lock rejects concurrent processes sharing
one store. Four jobs may wait behind the active job. Hercules gets a fresh project
folder and process group for each case; its installed package is unchanged.

## Contract version 1

`POST /runs` and `GET /runs/{runId}` require
`Authorization: Bearer <runner token>`. `GET /health` is public.

```json
{
  "schemaVersion": "1",
  "requestId": "a-new-unique-submission-id",
  "targetUrl": "http://127.0.0.1:8080/contract",
  "loginUsername": "",
  "loginPassword": "",
  "timeoutSeconds": 600,
  "cases": [{
    "caseId": 123,
    "caseCode": "CASE-example",
    "name": "Read-only health check",
    "precondition": "Public endpoint; no authentication required",
    "steps": "请求：GET /api/health\n期望状态码：200\n断言：$.code=0",
    "expectedResult": "Health check succeeds"
  }]
}
```

Submission returns HTTP 202 with `runId`, `state`, `createdAt`, and `caseResults`.
Polling adds `startedAt`, `finishedAt`, and per-case `state`, `durationMs`, and
`evidence`. States are `queued`, `running`, `passed`, `failed`, `error`, and
`timed_out`. Duplicate request IDs reuse the original job; changed test content
returns 409. Credentials are excluded from the persistent fingerprint, so changing
only credentials with the same request ID does **not** rerun the test. Use a new ID.
Queue overflow returns 429. Job timeout starts when the worker begins execution
and covers the whole batch, not each case separately.

The current platform adapter submits and polls in the existing synchronous
execution request. It then stores the run ID and outcome in 执行记录. The runner
itself is asynchronous and persistent. **Backend restart recovery and automatic
platform result reconciliation are not implemented**: a browser/backend disconnect
may leave a completed runner job without a saved platform record. Do not retry a
write case blindly. Backend polling waits at most the configured timeout plus 60
seconds, including queue wait; a queued job can outlive that wait.

## Evidence and verdicts

Hercules receives a generated Gherkin scenario, the full case text, the target entry
URL, and temporary test data containing the supplied login. It is instructed to
find missing IDs through the UI/API and stop if it cannot identify a disposable
test record. This is agent behavior, not a deterministic deletion safeguard.
Use dedicated test data. Credentials may be processed by the configured model.

A pass requires completion or an explicit final structured assertion, plus a
matching **original** API response. XML without a failure or CLI exit code 0 alone
is insufficient. The installed Hercules defaults `is_completed` to false in some
successful reports; the adapter also reads the final `is_assert/is_passed` fields
from `agent_inner_thoughts.json`.

Version 1 deterministically verifies one declared HTTP request per case, one
expected status, and simple `$.field`, nested fields, and `[index]` equality
assertions. The captured origin/method/path must match. Missing/ambiguous captured
responses and unsupported assertion formats produce `error`; requests are never
replayed for verification. Broader natural-language expectations remain evaluated
by Hercules, not independently verified by this adapter. Supply exact follow-up
query expectations for deletion checks. An entry page such as `/contract` is
preserved for Hercules; API origin matching uses the URL's origin.

Persistent evidence contains only fixed reason text, assertion outcomes, HTTP
status and a SHA-256 hash of the response. Raw response bodies, cookies, tokens,
agent conversations and screenshots are **not** served or retained as reports.
This limits detailed debugging but avoids storing target secrets in reports.
Hercules raw artifacts and credentials exist temporarily in mode-0700 scratch
folders (credential file mode 0600); they are deleted on completion. Startup
cleans abandoned scratch folders and marks interrupted jobs `error`. It kills an
orphan CLI group only when the saved PID/start time still identifies that process.
Use graceful shutdown; abrupt machine/process failure can leave artifacts until
the next startup. SQLite contains job metadata/results, not submitted credentials.

Platform compatibility mapping: `passed → PASSED`, `failed → FAILED`,
`error/timed_out → SKIPPED` with the actual runner state in details. Infrastructure
errors do not create assertion-failure issues. A mixed batch containing skipped
cases no longer reports SUCCESS.

## Checks and troubleshooting

```bash
.venv/bin/python -m unittest discover -s . -v
# With runner and target system running:
.venv/bin/python smoke.py
```

`smoke.py` invokes the real Hercules CLI against the supplied target's read-only
`http://127.0.0.1:8080/api/health`; it checks an expected pass and intentional
HTTP-status failure. Unit-test fixtures are not evidence of Hercules integration.

- Runner unavailable: check `runner.log`, `/health`, and the shared token; platform
  records a skipped/error detail rather than a fabricated pass.
- Missing evidence: check that Hercules used its API helper and the case has the
  supported explicit request/assertion syntax.
- Authentication: provide target credentials in the dialog, not the platform's
  credentials unless both systems deliberately share the same identity provider.
- `127.0.0.1` refers to the runner's host. Container/remote deployments need a
  reachable target address. This version binds the bridge to loopback; LAN/TLS
  deployment and another-machine acceptance are not yet validated.
- Retention: job metadata is retained until an operator archives/removes the store
  while the runner is stopped. There is no automatic retention scheduler yet.

## Historical acceptance in the platform checkout, 2026-10-28

- Runner tests: 11 passed; backend adapter tests: 3 passed.
- Backend offline package build passed; execution-dialog ESLint passed.
- Full Vue type checking remains blocked by existing Monaco editor typing errors
  in repository views, outside this change.
- Real CLI positive run `d88beaed-ce81-4c7f-820e-0de2007abeea`: passed, 10,999 ms;
  original response HTTP 200 and JSON code=0 verified.
- Real CLI negative run `8d3f81b3-8077-4a1f-9e7c-06c623d2acf1`: failed, 12,199 ms;
  intentionally expected HTTP 201 from the same read-only health endpoint.
- Target login, contract ID discovery, deletion and a complete browser-button
  execution through the platform have not yet been acceptance-tested.

## Failure diagnostics

New jobs retain a bounded list of API method/status pairs classified as login,
contract list, contract detail, or other. Explicit failure language is converted
to fixed model-reported hints (authentication, missing test data, connection,
timeout, assertion mismatch). These hints are not independent proof of cause.
The platform shows this summary in the existing execution detail. Raw URLs,
headers, bodies, credentials and free-form model output are still discarded.
Existing jobs cannot be enriched after their scratch artifacts were deleted.

## Request correlation

New runs instruct Hercules to add a unique `X-Hercules-Request-Id` header only
to the request under test, after authentication and discovery are complete.
The verifier requires one marked request and one matching original response;
discovery responses cannot be selected instead. Duplicate/missing markers,
wrong origins and mismatched paths remain errors. Missing responses and multiple
responses now have separate diagnostics. No HTTP requests are replayed.
Query-only scenarios instruct the agent not to create or modify business data
unless the case steps explicitly request it. This instruction is not a network
write-blocking mechanism. Existing run verdicts are not retroactively changed.

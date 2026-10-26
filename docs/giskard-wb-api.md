# Code-Agent Instructions: Giskard Training-Run API

Implement a **Python HTTP API server** for creating and controlling remote Giskard training runs. Use **SkyPilot managed jobs on Lambda.ai** for execution and **authenticated MinIO** for input and output objects. Talk to SkyPilot through its Python SDK, its only supported client. The SDK is synchronous, so keep its calls off the request-handling event loop. A run specifies a Python trainer wheel, dataset, training configuration, and GPU requirements; it does not require an existing model ID.

## Run data

Persist each run with a server-generated ID, name, owner, timestamps, and:

- **Trainer:** MinIO object key for an existing `.whl` file.
- **Dataset:** MinIO bucket/prefix or object key.
- **Configuration:** training configuration (JSON/YAML, or a MinIO object reference).
- **Compute:** requested GPU type/count and optional runtime limit.
- **Results:** server-assigned MinIO output prefix.
- **Execution:** run state, SkyPilot job ID (when assigned), and last error/status message.

Use the project's existing persistence conventions where possible. Prevent changes to a run's inputs while it is executing.

## HTTP API

Use JSON request/response bodies and conventional HTTP status codes. Prefix endpoints with `/api/runs`.

| Method | Endpoint | Purpose |
|---|---|---|
| `POST` | `/api/runs` | Create a run definition |
| `GET` | `/api/runs` | List runs accessible to the caller |
| `GET` | `/api/runs/:id` | Get run definition and current state |
| `PATCH` | `/api/runs/:id` | Update a run definition before execution |
| `DELETE` | `/api/runs/:id` | Delete an inactive run definition |
| `POST` | `/api/runs/:id/start` | Submit the training job to SkyPilot |
| `POST` | `/api/runs/:id/stop` | Request a graceful stop, where supported |
| `POST` | `/api/runs/:id/abort` | Cancel the SkyPilot job promptly |
| `GET` | `/api/runs/:id/status` | Return current job state and error information |
| `GET` | `/api/runs/:id/logs` | Retrieve training-job logs |
| `POST` | `/api/runs/:id/collect` | Refresh the run's result listing from MinIO |
| `GET` | `/api/runs/:id/results` | List available output objects and provide authorized retrieval links |

`start`, `stop`, `abort`, and `collect` should respond promptly; callers can poll `status`. Expose straightforward states such as `draft`, `queued`, `running`, `stopping`, `stopped`, `succeeded`, `failed`, `aborting`, and `aborted`. Report uncertainty or failed cancellation rather than claiming a job has stopped when cleanup is unconfirmed.

## Authentication and authorization

- **Require OIDC-based identity** for all training-run endpoints. Accept OAuth2 bearer **access tokens** issued for this API; validate issuer, audience, signature (using the issuer's JWKS), and expiry. Do not accept an ID token as an API access token.
- Associate runs with the authenticated user's stable `(issuer, subject)` identity. A caller may access or modify their own runs; allow an optional configured administrator role/scope. Enforce authorization on every endpoint, including logs and results.
- Keep Lambda.ai, SkyPilot, and MinIO credentials on the server. Never return them to the browser or include them in logs. Configure allowed browser origins explicitly. Read all settings from a YAML config file (a mounted ConfigMap in Kubernetes) and credentials from mounted Secret files, not from environment variables.

## Server behavior

- On `start`, generate a SkyPilot managed-job specification from the saved run inputs, submit it on Lambda.ai, and record the resulting job identifier. The remote job should install the referenced wheel, use the selected dataset and config, and write outputs under the run's MinIO results prefix.
- Obtain job status and logs through SkyPilot. `stop` requests graceful shutdown where the trainer/job supports it; `abort` issues cancellation without waiting for a new checkpoint. Keep these operations distinct and report their actual outcomes.
- `collect` and `results` inspect the run's MinIO output prefix. Return only objects belonging to a run the caller is authorized to access.
- Persist run definitions and job IDs so the API can resume status tracking after a server restart. Avoid duplicate submissions when requests are retried.
- Validate request fields, restrict GPU selections and MinIO locations to server-approved values, and ensure client input cannot become arbitrary shell commands.

## Completion criteria

Provide the working API server, its configuration example, and basic endpoint tests (including OIDC authorization and mocked SkyPilot/MinIO interactions). An authorized caller must be able to create a run referencing an existing wheel and dataset, start it, check status/logs, stop or abort it, and retrieve results through the API.

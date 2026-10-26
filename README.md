# giskard-wb-api — Giskard training-run API

A Python (FastAPI) HTTP API for creating and controlling Giskard training runs.
Runs execute as **SkyPilot managed jobs on Lambda.ai**. Inputs and outputs live in
**MinIO**. Callers authenticate with **OIDC bearer access tokens**. The spec is in
[docs/giskard-wb-api.md](docs/giskard-wb-api.md).

To deploy it on Kubernetes with Keycloak and MinIO and run a first job, follow
[QUICKSTART.md](QUICKSTART.md).

## Quick start

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[test]'            # add ,local-sky to run a SkyPilot API server locally
cp config.example.yaml config.yaml  # then fill in OIDC, MinIO, allowlists, SkyPilot endpoint
giskard-wb-api --config config.yaml # or: python -m giskard_wb_api --config config.yaml
pytest
```

Requires Python ≥ 3.11. SkyPilot is pinned to 0.14.0, the same release as the Helm chart's
SkyPilot dependency.

### Docker image

Build settings live in `config.mk`, which is not committed. Run `make config` to create it from
`config.mk.example`, then edit it. Any setting given on the command line overrides `config.mk`.

```bash
make config                                 # create config.mk; set REGISTRY etc. there
make build                                  # giskard-wb-api:<pyproject.toml version> and :latest
make push                                   # build, then push (REGISTRY from config.mk)
make push REGISTRY=ghcr.io/your-org TAG=dev # one-off override
docker run -p 8080:8080 -v giskard-data:/data \
  -v "$PWD/config.yaml:/etc/giskard-wb-api/config.yaml:ro" ghcr.io/your-org/giskard-wb-api:latest
```

The image reads `/etc/giskard-wb-api/config.yaml` unless you pass `--config`. It contains the
SkyPilot client only, so `skypilot.api_server_endpoint` must point at a SkyPilot API server that
has the Lambda.ai credentials. Set `server.database_path` to a file under the `/data` volume, e.g.
`/data/giskard-wb-api.db`.

### Configuration

All settings come from one YAML file, passed with `--config` (default
`/etc/giskard-wb-api/config.yaml`). [`config.example.yaml`](config.example.yaml) documents every
key. The server does not read environment variables. It validates the whole file at startup and
refuses to start if a key is unknown, missing or invalid, listing every problem.

Credentials (`minio.access_key`, `minio.secret_key`, `minio.job_access_key`,
`minio.job_secret_key`, `skypilot.service_account_token`) can be written inline, which is
convenient for local development. Alternatively, put each one in its own file in `secrets_dir`,
named by its config key (for example `secrets/minio.secret_key`). A credential may not be given
both ways.

### Kubernetes (Helm)

The chart in `chart/` deploys the server:

- `values.config` is rendered into a ConfigMap and mounted as `/etc/giskard-wb-api/config.yaml`.
  Changing it rolls the pod.
- Credentials come from a Secret mounted as files at `/etc/giskard-wb-api/secrets`, the server's
  `secrets_dir`. Its keys are the config keys they set (`minio.access_key`, `minio.secret_key`,
  and so on). Use `secrets.existingSecret`, or let the chart create one from `secrets.values`.
- The pod has no environment variables for the server; the chart passes
  `--config /etc/giskard-wb-api/config.yaml`.
- The database is stored on a PVC at `/data`.
- The Ingress gets its TLS certificate from cert-manager. Set the issuer with
  `ingress.certManager.issuer` and `ingress.certManager.issuerKind`.
- The chart runs exactly one replica with the `Recreate` strategy, because of SQLite.
- The official SkyPilot chart (`skypilot`, from https://helm.skypilot.co, version 0.14.0) is a
  dependency. It deploys the SkyPilot API server, which holds the Lambda.ai key and runs the
  managed-jobs controller. giskard-wb-api stays a lightweight client of it.
  - SkyPilot is limited to Lambda.ai. The jobs controller runs inside the API server pod
    (`jobs.controller.consolidation_mode`), not as a separate VM on Lambda. SkyPilot gets no
    Kubernetes access and creates no RBAC, so installing needs only namespace-level rights.
  - When installing without `make install`, first run `helm repo add skypilot https://helm.skypilot.co`
    and `helm dependency build chart`.
  - The API server needs the `lambda-credentials` Secret (key `api_key`). Create it yourself, or
    set `lambdaCredentials.create=true` and `lambdaCredentials.apiKey`.
  - The SkyPilot dashboard is not exposed by default, and in-cluster requests to SkyPilot are not
    authenticated. On a shared cluster, set `networkPolicy.enabled=true` so only giskard-wb-api pods can
    reach the API server. For debugging, use
    `kubectl port-forward svc/<release>-api-service 46580:80`. To expose the dashboard, enable `skypilot.ingress` together with
    `skypilot.auth.oauth` (OIDC). Then create a SkyPilot service account token and store it as
    `skypilot.service_account_token` in giskard-wb-api's Secret.
  - To use an existing SkyPilot deployment instead, set `skypilot.enabled=false` and
    `config.skypilot.api_server_endpoint`.
  - Keep the subchart version and `skypilot` in `pyproject.toml` on the same release.

```bash
cp values.yaml.example values.yaml                  # edit: image, OIDC, MinIO, hosts, secrets
kubectl create namespace giskard
kubectl -n giskard create secret generic lambda-credentials --from-literal api_key=<lambda-api-key>
make install                                        # helm upgrade --install, see below
helm test giskard-wb-api -n giskard
```

`make install` registers the SkyPilot Helm repository if needed, fetches the chart dependencies
pinned in `chart/Chart.lock`, and runs `helm upgrade --install`. The release name, namespace,
values file and extra Helm arguments are `RELEASE`, `NAMESPACE`, `VALUES` and `HELM_ARGS`, set in
`config.mk` or on the command line (e.g. `make install HELM_ARGS="--wait --kube-context prod"`).
When `REGISTRY` is set, it deploys the image `make push` publishes (`IMAGE_NAME`:`TAG`),
overriding `image.*` in the values file.

## Architecture

```
HTTP (FastAPI) ── auth.py: OIDC access-token validation (PyJWT, issuer JWKS)
   │
   └─ runs/routes.py → runs/service.py (state machine, authorization)
                          ├─ runs/store.py       SQLite: run definitions, job IDs, state
                          ├─ storage.py          MinIO via S3 API (boto3): checks, listing, presigned links
                          └─ skypilot/client.py  SkyPilot SDK, called on a thread pool
                               └─ skypilot/task.py builds the managed-job task (fixed scripts + env vars)
```

The server is asyncio-based. The SkyPilot SDK and boto3 are synchronous, so their calls run in
worker threads and the event loop keeps serving requests:

- SkyPilot calls use a dedicated pool of `skypilot.threads` threads. Each call is limited to
  `skypilot.request_timeout_seconds`. A call that times out is reported as failed, but its thread can't
  be interrupted and finishes in the background.
- A launch request returns immediately. The server then polls the request's status every 5 s,
  for up to `skypilot.launch_timeout_seconds`, instead of holding a thread while the GPU node is
  provisioned.

A poller (`runs.status_poll_interval_seconds`) reconciles active runs against SkyPilot. Every
run's state, launch request ID and job ID are persisted, so a restarted server picks up
where it left off.

## API

All endpoints need `Authorization: Bearer <access token>`. Callers only see their own runs,
keyed by `(iss, sub)`. Administrators can see and control every run: the role is
`oidc.admin_role` and the scope is `oidc.admin_scope`. A run the caller may not access
returns `404`.

| Method | Path | Notes |
|---|---|---|
| `POST` | `/api/runs` | Create a draft. `201` |
| `GET` | `/api/runs?limit&offset&mine=true` | List runs |
| `GET` | `/api/runs/:id` | Full definition and state |
| `PATCH` | `/api/runs/:id` | Only in `draft`, otherwise `409` |
| `DELETE` | `/api/runs/:id` | Only in `draft` or a terminal state. `204` |
| `POST` | `/api/runs/:id/start` | `202`. Idempotent: retries never submit twice |
| `POST` | `/api/runs/:id/stop` | `202`. Graceful stop (see below) |
| `POST` | `/api/runs/:id/abort` | `202`. Cancels the SkyPilot job |
| `GET` | `/api/runs/:id/status` | State, SkyPilot status, errors, cancellation outcome |
| `GET` | `/api/runs/:id/logs?tail=1000` | Job logs from SkyPilot |
| `POST` | `/api/runs/:id/collect` | `202`. Re-list outputs from MinIO in the background |
| `GET` | `/api/runs/:id/results` | Last collected listing, with presigned GET URLs |

Create body:

```json
{
  "name": "sentiment fine-tune",
  "trainer": { "bucket": "giskard-artifacts", "key": "wheels/my_trainer-1.0.0-py3-none-any.whl", "entrypoint": "my_trainer" },
  "dataset": { "bucket": "giskard-datasets", "prefix": "reviews/" },
  "config":  { "format": "json", "value": { "epochs": 3 } },
  "compute": { "gpuType": "H100", "gpuCount": 8, "maxRuntimeMinutes": 240 }
}
```

- `dataset` can be `{ bucket, prefix }` or `{ bucket, key }`.
- `config` can be `{ format: "json", value: {...} }`, `{ format: "yaml", value: "..." }`, or
  `{ object: { bucket, key } }`.
- Buckets and prefixes must fall inside the `storage.allowed_*_locations` allowlists.
- GPU type and count must be listed in `runs.gpus`.
- The wheel, dataset and config object must exist. This is checked when the run is
  created or updated, and again at `start`.

### States

`draft → submitting → queued → running → succeeded | failed`, plus:

- `stopping → stopped` after a stop request.
- `aborting → aborted` after an abort.

| Field | Meaning |
|---|---|
| `statusUncertain: true` | The last SkyPilot check failed or the job could not be found. `state` may be stale. |
| `cancellation` | Whether SkyPilot accepted the cancel. |
| `lastError` | The most recent error. |

A run only becomes `aborted` once SkyPilot reports `CANCELLED`. If the cancel call
fails, the run stays `aborting`, the error is recorded, and calling `abort` again
retries it.

## Trainer contract (the remote job)

**Setup.** The job creates a venv with `--system-site-packages`, so the Lambda stack's
PyTorch stays visible. It then downloads and `pip install`s the wheel.

**Run.** The job downloads the dataset and config, then runs:

```
python -m <entrypoint> --dataset <dir> --config <file> --output <dir>
```

The same paths are also available as `GISKARD_DATASET_DIR`, `GISKARD_CONFIG_PATH` and
`GISKARD_OUTPUT_DIR`. When the trainer exits, everything in the output directory is
uploaded to `<storage.results_location>/<run-id>/outputs/`. This happens even when training
fails, so partial checkpoints are kept.

**Graceful stop.** `stop` writes a marker object to `<run>/control/STOP`. The job checks
for it every 15 s. When it finds the marker, the job sends the trainer `SIGTERM` and then
`SIGKILL` after `runs.stop_grace_seconds`. Outputs are uploaded either way. The trainer should
handle `SIGTERM` by writing a checkpoint and exiting with code 0 (code 143 is also
accepted). A job that exits cleanly after a stop ends as `stopped`.

**Abort.** Calls `sky jobs cancel` and does not wait for a checkpoint.

**Runtime limit.** When `maxRuntimeMinutes` is reached, the trainer gets the same
SIGTERM-then-SIGKILL sequence, and the job exits with code 124 (state `failed`).

## Security notes

- Lambda.ai, SkyPilot and MinIO credentials stay on the server.
  - The job's MinIO key goes to SkyPilot as a task `secret`, which SkyPilot redacts.
  - SkyPilot errors and logs are scrubbed of the configured keys before they are stored
    or returned.
  - Request logs redact `Authorization`.
- Client input never becomes shell syntax.
  - The setup and run scripts are constants. Run values reach them only as
    always-quoted environment variables.
  - Object keys, module names and GPU types are validated against strict patterns
    and allowlists.
- ID tokens are rejected in two ways:
  - The audience check: ID tokens are issued for the client ID, not the API.
  - Explicit checks for `nonce`, `at_hash`, `typ: ID` and an `id_token+jwt` header.
  - Set `oidc.require_at_jwt_type: true` if your IdP issues RFC 9068 tokens.
- Results are listed only under the run's own prefix. Download links are presigned
  against `minio.public_endpoint` and expire after `storage.presign_ttl_seconds`.

## Limitations

- This is designed for a single server instance. SQLite runs in WAL mode, and background
  work is de-duplicated within one process.
- If the server crashes before SkyPilot acknowledges a launch request, the run is not
  resubmitted, to avoid creating duplicate jobs. The server looks for the job by its
  deterministic name (`giskard-<run-id>`). If it finds no job within 2 minutes, it marks
  the run `failed`.
- To retry a finished run, create a new one. `start` only accepts `draft` runs.

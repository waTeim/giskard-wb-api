# Quickstart: deploy giskard-wb-api on Kubernetes

This guide takes you from an empty namespace to a finished training run. You set up Keycloak and
MinIO, build and push the image, install the Helm chart, then run a small trainer on a Lambda.ai
GPU through the API. The [README](README.md) explains each piece in more depth.

## What you need

- A Kubernetes cluster with [ingress-nginx](https://kubernetes.github.io/ingress-nginx/) and
  [cert-manager](https://cert-manager.io/), and a `ClusterIssuer` named `letsencrypt-prod`.
- Room for the SkyPilot API server, which the chart deploys. By default it requests 4 CPUs and
  8 GiB of memory.
- A Keycloak server, with admin access.
- A MinIO server reachable from the cluster and from the internet, because Lambda.ai GPU nodes
  download inputs and upload outputs directly. You need admin access.
- A Lambda.ai API key.
- On your machine: `kubectl`, `helm` 3, `docker`, `make`, `jq`, MinIO's `mc` client and
  Python 3.11 or later.

The examples use these hostnames. Replace them everywhere, in this guide and in the files you copy:

| Placeholder | What it is |
|---|---|
| `giskard-api.example.com` | This API (the Ingress host) |
| `giskard.example.com` | The Giskard web UI, which calls the API from the browser |
| `auth.example.com` | Keycloak. The realm's issuer is `https://auth.example.com/realms/giskard` |
| `minio.example.com` | MinIO's public S3 endpoint |
| `minio.minio.svc.cluster.local:9000` | MinIO's in-cluster S3 endpoint |
| `ghcr.io/your-org` | Your container registry |

## 1. Keycloak

The server needs a realm whose access tokens carry the API's audience and the users' realm roles.
The settings below match the OIDC configuration in step 5. Names are only conventions; if you
change one, change the matching server setting too.

**Realm:** `giskard`. Its issuer, `https://auth.example.com/realms/giskard`, is `oidc.issuer`.
The default access-token lifespan of 5 minutes is fine.

**Realm role:** `giskard-admin`, which is `oidc.admin_role`. Holders can see and control every
user's runs; everyone else sees only their own. Granting it through a group such as
`giskard-admins` keeps membership easy to manage.

**Client `giskard-web`,** for the Giskard web UI:

| Setting | Value |
|---|---|
| Client authentication | Off (public client) |
| Authentication flow | Standard flow only (authorization code). No direct access grants or implicit flow |
| PKCE method | `S256` (Advanced → Proof Key for Code Exchange) |
| Valid redirect URIs | `https://giskard.example.com/*` |
| Valid post-logout redirect URIs | `https://giskard.example.com/*` |
| Web origins | `https://giskard.example.com`. Also list it in `server.cors_allowed_origins` |

**Client `giskard-cli`,** for scripts and the test run in step 7:

| Setting | Value |
|---|---|
| Client authentication | On (confidential client) |
| Authentication flow | Service accounts roles only (client credentials grant) |
| Credentials | Keep the generated client secret for step 7 |

To give scripts administrator access, assign `giskard-admin` to the client's service account
(the **Service account roles** tab).

**Audience mapper, on both clients:** a mapper of type **Audience** (in the client's dedicated
scope) with **Included Custom Audience** set to `giskard-api`, and **Add to access token** on.
This is `oidc.audience`. Without it, Keycloak's access tokens are issued for `account` and the
server rejects them.

**Roles claim:** nothing to configure. Keycloak's default `roles` client scope puts realm roles in
`realm_access.roles`, which is `oidc.roles_claim`. Leave that scope assigned to both clients.

**Token type:** Keycloak's access tokens have the header `typ: JWT`, not `at+jwt`, so leave
`oidc.require_at_jwt_type` set to `false`. The server still rejects Keycloak ID tokens, because
their audience is the client ID and because they carry `typ: ID`.

**Users:** create users with a password. To make someone an administrator, give them
`giskard-admin`, directly or through the `giskard-admins` group.

The server downloads Keycloak's signing keys from
`https://auth.example.com/realms/giskard/.well-known/openid-configuration`, so that URL must be
reachable from inside the cluster. If it isn't, set `oidc.jwks_uri` to an in-cluster URL for
`…/realms/giskard/protocol/openid-connect/certs`. `oidc.issuer` must still be the public URL,
because it has to match the tokens' `iss` claim exactly.

## 2. MinIO

Create the buckets, an access policy, and two users: one for the server and one for training
jobs. The job's key is sent to the GPU nodes, so keep it separate from the server's key.

```bash
mc alias set giskard https://minio.example.com <admin-user> <admin-password>
mc mb giskard/giskard-artifacts giskard/giskard-datasets giskard/giskard-results

cat > giskard-wb-api-policy.json <<'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": ["arn:aws:s3:::giskard-artifacts", "arn:aws:s3:::giskard-datasets", "arn:aws:s3:::giskard-results"]
    },
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject"],
      "Resource": ["arn:aws:s3:::giskard-artifacts/*", "arn:aws:s3:::giskard-datasets/*", "arn:aws:s3:::giskard-results/*"]
    },
    {
      "Effect": "Allow",
      "Action": ["s3:PutObject"],
      "Resource": ["arn:aws:s3:::giskard-results/runs/*"]
    }
  ]
}
EOF
mc admin policy create giskard giskard-wb-api giskard-wb-api-policy.json

SERVER_SECRET=$(openssl rand -hex 24); JOB_SECRET=$(openssl rand -hex 24)
mc admin user add giskard giskard-wb-api "$SERVER_SECRET"
mc admin user add giskard giskard-job "$JOB_SECRET"
mc admin policy attach giskard giskard-wb-api --user giskard-wb-api
mc admin policy attach giskard giskard-wb-api --user giskard-job
```

Both users need the same access:
- **Server:** checks that inputs exist, writes each run's inline config and stop marker under
  `giskard-results/runs/`, and signs download links for results.
- **Training job:** reads the inputs and writes results.

## 3. Build and push the image

```bash
make config                  # creates config.mk
# edit config.mk: REGISTRY = ghcr.io/your-org
make push                    # builds and pushes ghcr.io/your-org/giskard-wb-api:<version> and :latest
```

Because `REGISTRY` is set, `make install` in step 6 deploys this image. You don't need to set
`image` in the values file.

## 4. Kubernetes Secrets

```bash
kubectl create namespace giskard

# MinIO keys for the server (from step 2). Each key name is the config key it sets.
kubectl -n giskard create secret generic giskard-wb-api-credentials \
  --from-literal minio.access_key=giskard-wb-api \
  --from-literal minio.secret_key="$SERVER_SECRET" \
  --from-literal minio.job_access_key=giskard-job \
  --from-literal minio.job_secret_key="$JOB_SECRET"

# Lambda.ai API key for the bundled SkyPilot API server.
kubectl -n giskard create secret generic lambda-credentials --from-literal api_key=<lambda-api-key>
```

## 5. values.yaml

Save this as `values.yaml` in the repository root, then replace the hostnames. Settings not shown
here keep the defaults in [`chart/values.yaml`](chart/values.yaml).
[`values.yaml.example`](values.yaml.example) shows more of the options. `config` uses the same
layout as [`config.example.yaml`](config.example.yaml).

```yaml
config:
  server:
    log_level: info
    # The web UI's origin (the giskard-web client in Keycloak).
    cors_allowed_origins: [https://giskard.example.com]

  # Matches the Keycloak realm in step 1.
  oidc:
    issuer: https://auth.example.com/realms/giskard
    audience: [giskard-api]
    admin_role: giskard-admin
    roles_claim: realm_access.roles
    require_at_jwt_type: false
    # Only if the issuer URL is not reachable from inside the cluster:
    # jwks_uri: http://keycloak.keycloak.svc.cluster.local:8080/realms/giskard/protocol/openid-connect/certs

  minio:
    endpoint: http://minio.minio.svc.cluster.local:9000   # used by the server
    public_endpoint: https://minio.example.com            # in download links for browsers
    job_endpoint: https://minio.example.com               # used by Lambda.ai GPU nodes

  storage:
    allowed_trainer_locations: [giskard-artifacts/wheels]
    allowed_dataset_locations: [giskard-datasets]
    allowed_config_locations: [giskard-artifacts/configs]
    results_location: giskard-results/runs

  runs:
    gpus:
      H100: [1, 2, 4, 8]
      A100-80GB: [1, 2, 4, 8]
      A10: [1]
    max_runtime_minutes: 1440

# Created in step 4; mounted as files, not environment variables.
secrets:
  existingSecret: giskard-wb-api-credentials

ingress:
  enabled: true
  className: nginx
  certManager:
    enabled: true
    issuer: letsencrypt-prod
    issuerKind: ClusterIssuer
  hosts:
    - host: giskard-api.example.com
      paths:
        - path: /
          pathType: Prefix

# The SkyPilot API server reads the Lambda.ai key from this Secret (step 4).
lambdaCredentials:
  create: false

skypilot:
  enabled: true
```

## 6. Install

```bash
make install HELM_ARGS="--wait --timeout 15m"
helm test giskard-wb-api -n giskard
kubectl -n giskard get certificate                      # wait for READY=True
curl https://giskard-api.example.com/healthz            # {"ok":true}
```

`make install` uses release `giskard-wb-api`, namespace `giskard` and `values.yaml` by default;
set `RELEASE`, `NAMESPACE` or `VALUES` in `config.mk` to change them. The first install can take
several minutes while the SkyPilot API server starts.

If the server pod keeps restarting, check its log with
`kubectl -n giskard logs deploy/giskard-wb-api`. A problem in the configuration is reported at
startup with the exact key, for example `oidc.issuer: Field required`.

## 7. Run a training job

The steps below build a tiny trainer, upload it with a dataset, and run it on one A10 GPU. A real
trainer follows the same contract: it is run as `python -m <entrypoint> --dataset DIR --config
FILE --output DIR`, and everything it writes to the output directory is uploaded to MinIO when it
exits.

**Build and upload the trainer and a dataset:**

```bash
mkdir -p hello-trainer/hello_trainer && touch hello-trainer/hello_trainer/__init__.py
cat > hello-trainer/pyproject.toml <<'EOF'
[build-system]
requires = ["setuptools>=69"]
build-backend = "setuptools.build_meta"

[project]
name = "hello-trainer"
version = "0.1.0"
EOF
cat > hello-trainer/hello_trainer/__main__.py <<'EOF'
import argparse, json, pathlib

p = argparse.ArgumentParser()
p.add_argument('--dataset'); p.add_argument('--config'); p.add_argument('--output')
args = p.parse_args()
files = sorted(str(f.relative_to(args.dataset)) for f in pathlib.Path(args.dataset).rglob('*') if f.is_file())
config = pathlib.Path(args.config).read_text()
summary = {'dataset_files': files, 'config': config}
pathlib.Path(args.output, 'summary.json').write_text(json.dumps(summary, indent=2))
print('wrote', pathlib.Path(args.output, 'summary.json'))
EOF
pip wheel --no-deps -w dist ./hello-trainer

mc cp dist/hello_trainer-0.1.0-py3-none-any.whl giskard/giskard-artifacts/wheels/
printf 'text,label\ngreat,1\nawful,0\n' > train.csv
mc cp train.csv giskard/giskard-datasets/hello/train.csv
```

**Get an access token** with the `giskard-cli` secret from step 1:

```bash
API=https://giskard-api.example.com
TOKEN=$(curl -s https://auth.example.com/realms/giskard/protocol/openid-connect/token \
  -d grant_type=client_credentials -d client_id=giskard-cli -d client_secret='<giskard-cli secret>' \
  | jq -r .access_token)
H=(-H "authorization: Bearer $TOKEN" -H 'content-type: application/json')
curl -s "${H[@]}" "$API/api/runs" | jq .        # an empty list means auth works
```

Tokens expire after 5 minutes, Keycloak's default access-token lifespan. Run the `TOKEN=` command
again when calls start returning `401`.

**Create and start a run:**

```bash
RUN=$(curl -s "${H[@]}" "$API/api/runs" -d '{
  "name": "hello",
  "trainer": {"bucket": "giskard-artifacts", "key": "wheels/hello_trainer-0.1.0-py3-none-any.whl", "entrypoint": "hello_trainer"},
  "dataset": {"bucket": "giskard-datasets", "prefix": "hello/"},
  "config":  {"format": "json", "value": {"epochs": 1}},
  "compute": {"gpuType": "A10", "gpuCount": 1, "maxRuntimeMinutes": 30}
}' | jq -r .id)
curl -s -X POST "${H[@]}" "$API/api/runs/$RUN/start" | jq .
```

**Follow it.** Provisioning a GPU node usually takes a few minutes. The state goes from
`submitting` to `queued`, then `running`, then `succeeded`:

```bash
curl -s "${H[@]}" "$API/api/runs/$RUN/status" | jq '{state, skyStatus, message, lastError}'
curl -s "${H[@]}" "$API/api/runs/$RUN/logs?tail=200" | jq -r .logs
```

**Download the results.** The download links expire after `storage.presign_ttl_seconds` (default
900 s):

```bash
curl -s "${H[@]}" "$API/api/runs/$RUN/results" | jq '.objects[] | {key, url}'
curl -s "$(curl -s "${H[@]}" "$API/api/runs/$RUN/results" | jq -r '.objects[0].url')"
```

To stop a run early, call `POST /api/runs/$RUN/stop`. The trainer gets `SIGTERM` and its outputs
are still uploaded. To cancel the job outright, call `POST /api/runs/$RUN/abort`.

## Troubleshooting

Calls rejected with `401 Invalid access token` don't say why; that's deliberate. The server logs
the reason. Look in `kubectl -n giskard logs deploy/giskard-wb-api` for `rejected access token`.

| Symptom | Likely cause |
|---|---|
| Log: `InvalidAudienceError: Audience doesn't match` | The token has no `giskard-api` in `aud`. Check the client's audience mapper |
| Log: `InvalidIssuerError: Invalid issuer` | `oidc.issuer` doesn't exactly match the token's `iss` (scheme, host, `/realms/giskard`, no trailing slash) |
| Log: `OIDC discovery failed` or a JWKS URL error | The pod can't reach Keycloak. Set `oidc.jwks_uri` to an in-cluster URL |
| `401 Token expired` | Get a new token. Keycloak's default lifetime is 5 minutes |
| `502` when creating a run | The server can't reach MinIO at `minio.endpoint`, or its key is wrong |
| `400` … `is not in an approved MinIO location` | The bucket or prefix isn't in `storage.allowed_*_locations` |
| Run `failed` while downloading inputs | The GPU node can't reach `minio.job_endpoint`, or the job key lacks access |
| Run stays `submitting` | The SkyPilot API server isn't ready or has no Lambda.ai key. Check `kubectl -n giskard logs deploy/giskard-wb-api-api-server` |
| Browser calls blocked by CORS | Add the web UI origin to `server.cors_allowed_origins` |

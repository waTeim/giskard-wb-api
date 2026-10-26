"""Builds the SkyPilot managed-job task for a run.

The setup/run scripts below are constants. Every run-specific value reaches
the remote job only as an environment variable (always referenced quoted), so
client input can never become shell syntax. Storage credentials are passed as
SkyPilot `secrets`, which SkyPilot redacts from its own displays.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..config import Config
from ..runs.model import ObjectRef, Run


@dataclass(frozen=True)
class RunPaths:
    outputs_prefix: str
    control_prefix: str
    inputs_prefix: str
    stop_marker_key: str


def run_paths(config: Config, run_id: str) -> RunPaths:
    base = f'{config.results.prefix}{run_id}/'
    return RunPaths(
        outputs_prefix=f'{base}outputs/',
        control_prefix=f'{base}control/',
        inputs_prefix=f'{base}inputs/',
        stop_marker_key=f'{base}control/STOP',
    )


def job_name(config: Config, run_id: str) -> str:
    return f'{config.sky.job_name_prefix}-{run_id}'


# Minimal boto3 helper used on the remote node. Arguments come from env vars.
S3_HELPER = r'''
import os, sys, boto3
from botocore.config import Config
s3 = boto3.client("s3", endpoint_url=os.environ["GISKARD_S3_ENDPOINT"],
                  region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
                  config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 10}))
op, bucket, *args = sys.argv[1:]
if op == "exists":
    try:
        s3.head_object(Bucket=bucket, Key=args[0]); sys.exit(0)
    except s3.exceptions.ClientError:
        sys.exit(1)
elif op == "get":
    os.makedirs(os.path.dirname(args[1]) or ".", exist_ok=True)
    s3.download_file(bucket, args[0], args[1])
elif op == "get-prefix":
    prefix, dest = args
    n = 0
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            rel = obj["Key"][len(prefix):]
            if not rel or rel.endswith("/") or ".." in rel.split("/"):
                continue
            path = os.path.join(dest, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            s3.download_file(bucket, obj["Key"], path); n += 1
    print(f"downloaded {n} objects")
elif op == "put-dir":
    src, prefix = args
    n = 0
    for root, _, files in os.walk(src):
        for name in files:
            path = os.path.join(root, name)
            s3.upload_file(path, bucket, prefix + os.path.relpath(path, src).replace(os.sep, "/")); n += 1
    print(f"uploaded {n} objects")
elif op == "put-text":
    s3.put_object(Bucket=bucket, Key=args[0], Body=args[1].encode())
else:
    sys.exit(f"unknown op {op}")
'''

SETUP_SCRIPT = (
    r'''set -euo pipefail
mkdir -p ~/giskard/wheel
if [ ! -x ~/giskard/venv/bin/python ]; then
  python3 -m venv --system-site-packages ~/giskard/venv || {
    sudo apt-get update -q && sudo apt-get install -y -q python3-venv
    python3 -m venv --system-site-packages ~/giskard/venv
  }
fi
~/giskard/venv/bin/pip install -q --upgrade pip boto3
cat > ~/giskard/s3.py <<'GISKARD_S3_HELPER'
'''
    + S3_HELPER.strip()
    + r'''
GISKARD_S3_HELPER
WHEEL=~/giskard/wheel/"$(basename -- "$GISKARD_TRAINER_KEY")"
rm -f ~/giskard/wheel/*.whl
~/giskard/venv/bin/python ~/giskard/s3.py get "$GISKARD_TRAINER_BUCKET" "$GISKARD_TRAINER_KEY" "$WHEEL"
~/giskard/venv/bin/pip install -q "$WHEEL"
'''
)

RUN_SCRIPT = r'''set -uo pipefail
export PATH="$HOME/giskard/venv/bin:$PATH" VIRTUAL_ENV="$HOME/giskard/venv"
S3=~/giskard/s3.py
W=~/giskard/work
rm -rf "$W" && mkdir -p "$W/data" "$W/output"
export GISKARD_DATASET_DIR="$W/data" GISKARD_OUTPUT_DIR="$W/output" GISKARD_CONFIG_PATH="$W/$GISKARD_CONFIG_FILE"

stop_requested() { python "$S3" exists "$GISKARD_RESULTS_BUCKET" "$GISKARD_STOP_KEY"; }

if stop_requested; then
  echo "[giskard] stop requested before training started; exiting"
  exit 0
fi

echo "[giskard] downloading dataset"
if [ "$GISKARD_DATASET_KIND" = "prefix" ]; then
  python "$S3" get-prefix "$GISKARD_DATASET_BUCKET" "$GISKARD_DATASET_PATH" "$W/data" || exit 1
else
  python "$S3" get "$GISKARD_DATASET_BUCKET" "$GISKARD_DATASET_PATH" "$W/data/$(basename -- "$GISKARD_DATASET_PATH")" || exit 1
fi
echo "[giskard] downloading config"
python "$S3" get "$GISKARD_CONFIG_BUCKET" "$GISKARD_CONFIG_KEY" "$GISKARD_CONFIG_PATH" || exit 1

echo "[giskard] starting trainer: python -m $GISKARD_ENTRYPOINT"
python -m "$GISKARD_ENTRYPOINT" --dataset "$GISKARD_DATASET_DIR" --config "$GISKARD_CONFIG_PATH" --output "$GISKARD_OUTPUT_DIR" &
PID=$!
START=$SECONDS
STOPPED=0
LIMIT=0
KILLER=""
# Watch for a stop request or the runtime limit; either sends SIGTERM, then
# SIGKILL after the grace period.
while kill -0 "$PID" 2>/dev/null; do
  sleep 15
  [ -n "$KILLER" ] && continue
  if [ $((SECONDS - START)) -ge "$GISKARD_MAX_RUNTIME_SECONDS" ]; then
    echo "[giskard] runtime limit reached; sending SIGTERM to trainer (grace $GISKARD_STOP_GRACE_SECONDS s)"
    LIMIT=1
  elif stop_requested; then
    echo "[giskard] stop requested; sending SIGTERM to trainer (grace $GISKARD_STOP_GRACE_SECONDS s)"
    STOPPED=1
  else
    continue
  fi
  kill -TERM "$PID" 2>/dev/null
  ( sleep "$GISKARD_STOP_GRACE_SECONDS"; kill -KILL "$PID" 2>/dev/null ) &
  KILLER=$!
done
wait "$PID"
RC=$?
[ -n "$KILLER" ] && kill "$KILLER" 2>/dev/null
echo "[giskard] trainer exited with code $RC"

echo "[giskard] uploading outputs"
python "$S3" put-dir "$GISKARD_RESULTS_BUCKET" "$W/output" "$GISKARD_RESULTS_PREFIX"
UPLOAD_RC=$?
python "$S3" put-text "$GISKARD_RESULTS_BUCKET" "$GISKARD_CONTROL_PREFIX"exit.json \
  "{\"exitCode\": $RC, \"stopRequested\": $STOPPED, \"runtimeLimitReached\": $LIMIT, \"uploadExitCode\": $UPLOAD_RC}" || true

if [ "$LIMIT" = 1 ]; then echo "[giskard] training exceeded the runtime limit"; exit 124; fi
if [ "$STOPPED" = 1 ] && { [ "$RC" = 0 ] || [ "$RC" = 143 ]; }; then RC=0; fi
if [ "$RC" != 0 ]; then exit "$RC"; fi
if [ "$UPLOAD_RC" != 0 ]; then echo "[giskard] output upload failed"; exit 1; fi
'''


def build_task(config: Config, run: Run, config_object: ObjectRef) -> tuple[str, dict[str, Any]]:
    """Returns the job name and the SkyPilot task (YAML-config form).

    `config_object` is where the job reads its training config: an uploaded copy
    of inline config, or the referenced MinIO object.
    """
    paths = run_paths(config, run['id'])
    name = job_name(config, run['id'])
    runtime_minutes = run['compute'].get('maxRuntimeMinutes', config.max_runtime_minutes)
    ext = re.search(r'\.(json|ya?ml)$', config_object['key'], re.IGNORECASE)
    config_ext = ext.group(1).lower() if ext else 'txt'
    dataset = run['dataset']

    resources: dict[str, Any] = {
        'infra': f'{config.sky.infra}/{config.sky.region}' if config.sky.region else config.sky.infra,
        'accelerators': f'{run["compute"]["gpuType"]}:{run["compute"]["gpuCount"]}',
        'disk_size': config.sky.disk_size_gb,
    }
    if config.sky.image_id:
        resources['image_id'] = config.sky.image_id

    return name, {
        'name': name,
        'resources': resources,
        'envs': {
            'GISKARD_RUN_ID': run['id'],
            'GISKARD_S3_ENDPOINT': config.minio.job_endpoint,
            'AWS_DEFAULT_REGION': config.minio.region,
            'GISKARD_TRAINER_BUCKET': run['trainer']['bucket'],
            'GISKARD_TRAINER_KEY': run['trainer']['key'],
            'GISKARD_ENTRYPOINT': run['trainer']['entrypoint'],
            'GISKARD_DATASET_BUCKET': dataset['bucket'],
            'GISKARD_DATASET_KIND': 'object' if 'key' in dataset else 'prefix',
            'GISKARD_DATASET_PATH': dataset['key'] if 'key' in dataset else dataset['prefix'],
            'GISKARD_CONFIG_BUCKET': config_object['bucket'],
            'GISKARD_CONFIG_KEY': config_object['key'],
            'GISKARD_CONFIG_FILE': f'config.{config_ext}',
            'GISKARD_RESULTS_BUCKET': run['results']['bucket'],
            'GISKARD_RESULTS_PREFIX': paths.outputs_prefix,
            'GISKARD_CONTROL_PREFIX': paths.control_prefix,
            'GISKARD_STOP_KEY': paths.stop_marker_key,
            'GISKARD_MAX_RUNTIME_SECONDS': str(runtime_minutes * 60),
            'GISKARD_STOP_GRACE_SECONDS': str(config.stop_grace_seconds),
        },
        'secrets': {
            'AWS_ACCESS_KEY_ID': config.minio.job_access_key,
            'AWS_SECRET_ACCESS_KEY': config.minio.job_secret_key,
        },
        'setup': SETUP_SCRIPT,
        'run': RUN_SCRIPT,
    }

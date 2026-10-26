"""Run data model shared by the store, service and HTTP layer.

A run is stored and served as a JSON document (a plain dict with camelCase
keys). Optional fields are left out rather than set to None. The TypedDicts
below describe its shape.
"""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict

RunState = Literal[
    'draft', 'submitting', 'queued', 'running', 'stopping', 'stopped', 'succeeded', 'failed', 'aborting', 'aborted'
]

# States in which a SkyPilot job may exist and must be tracked.
ACTIVE_STATES: frozenset[str] = frozenset({'submitting', 'queued', 'running', 'stopping', 'aborting'})

TERMINAL_STATES: frozenset[str] = frozenset({'stopped', 'succeeded', 'failed', 'aborted'})


class ObjectRef(TypedDict):
    bucket: str
    key: str


class TrainerSpec(ObjectRef):
    # Python module run with `python -m`; defaults to the server's convention.
    entrypoint: str


# Either {bucket, prefix} or {bucket, key}.
DatasetSpec = dict[str, str]

# {format: 'json', value: {...}}, {format: 'yaml', value: '...'} or {object: ObjectRef}.
ConfigSpec = dict[str, Any]


class ComputeSpec(TypedDict):
    gpuType: str
    gpuCount: int
    maxRuntimeMinutes: NotRequired[int]


class Owner(TypedDict):
    issuer: str
    subject: str
    # Display name at creation time; informational only.
    name: NotRequired[str]


class Cancellation(TypedDict):
    requestedAt: str
    accepted: bool
    error: NotRequired[str]


class Execution(TypedDict):
    state: RunState
    # Human-readable status or error message from the last transition/check.
    message: NotRequired[str]
    lastError: NotRequired[str]
    skyJobName: NotRequired[str]
    skyRequestId: NotRequired[str]
    skyJobId: NotRequired[int]
    # Raw SkyPilot managed-job status from the last successful check.
    skyStatus: NotRequired[str]
    submittedAt: NotRequired[str]
    startedAt: NotRequired[str]
    finishedAt: NotRequired[str]
    lastCheckedAt: NotRequired[str]
    # True when the last status check failed, so `state` may be stale.
    statusUncertain: NotRequired[bool]
    stopRequestedAt: NotRequired[str]
    abortRequestedAt: NotRequired[str]
    # Outcome of the most recent cancellation call to SkyPilot.
    cancellation: NotRequired[Cancellation]


class ResultObject(TypedDict):
    key: str
    # Key relative to the run's results prefix.
    path: str
    size: int
    lastModified: NotRequired[str]
    etag: NotRequired[str]


class ResultsListing(TypedDict):
    collecting: bool
    collectedAt: NotRequired[str]
    objects: list[ResultObject]
    truncated: bool
    error: NotRequired[str]


class Run(TypedDict):
    id: str
    name: str
    owner: Owner
    createdAt: str
    updatedAt: str
    trainer: TrainerSpec
    dataset: DatasetSpec
    config: ConfigSpec
    compute: ComputeSpec
    results: dict[str, str]
    execution: Execution
    resultsListing: ResultsListing


def is_active(run: Run) -> bool:
    return run['execution']['state'] in ACTIVE_STATES

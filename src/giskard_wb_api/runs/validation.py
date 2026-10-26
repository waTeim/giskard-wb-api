"""Request body schemas. Values that end up in the remote job are restricted to
conservative character sets here, and to server-approved locations in the
service layer.

Request models are strict (no type coercion, no unknown fields). The inline
config size limit is configurable, so it is passed in the validation context:
`CreateRun.model_validate(body, context={'max_config_bytes': n})`.
"""

from __future__ import annotations

import json
import re
from typing import Annotated, Any, Literal

import yaml
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationInfo, model_validator

DEFAULT_ENTRYPOINT = 'giskard_trainer'

_BUCKET_RE = re.compile(r'^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$')
# No leading '/', no '.' or '..' segments, no '//' and no control characters.
_KEY_RE = re.compile(r'^(?!/)(?!.*//)(?!(?:.*/)?\.{1,2}(?:/|$))[^\x00-\x1f\x7f\\]+$', re.DOTALL)
_WHEEL_RE = re.compile(r'(^|/)[A-Za-z0-9_.+!-]+\.whl$')
_MODULE_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$')
_CONTROL_RE = re.compile(r'[\x00-\x1f\x7f]')


def _matches(pattern: re.Pattern[str], message: str) -> AfterValidator:
    def check(value: str) -> str:
        if not pattern.search(value):
            raise ValueError(message)
        return value

    return AfterValidator(check)


def _object_key(value: str) -> str:
    if not _KEY_RE.search(value):
        raise ValueError('invalid object key')
    if value.endswith('/'):
        raise ValueError('object key must not end with "/"')
    return value


def _prefix(value: str) -> str:
    if value and not (_KEY_RE.search(value) and value.endswith('/')):
        raise ValueError('prefix must be empty or a key prefix ending in "/"')
    return value


def _wheel(value: str) -> str:
    if not _WHEEL_RE.search(value):
        raise ValueError('trainer must be a .whl object with a valid wheel filename')
    return value


def _name(value: str) -> str:
    value = value.strip()
    if not 1 <= len(value) <= 128:
        raise ValueError('name must be 1 to 128 characters')
    if _CONTROL_RE.search(value):
        raise ValueError('name must not contain control characters')
    return value


Bucket = Annotated[StrictStr, _matches(_BUCKET_RE, 'invalid bucket name')]
ObjectKey = Annotated[StrictStr, Field(min_length=1, max_length=1024), AfterValidator(_object_key)]
Prefix = Annotated[StrictStr, Field(max_length=1024), AfterValidator(_prefix)]
Name = Annotated[StrictStr, AfterValidator(_name)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class ObjectRef(_Strict):
    bucket: Bucket
    key: ObjectKey


class Trainer(_Strict):
    bucket: Bucket
    key: Annotated[ObjectKey, AfterValidator(_wheel)]
    entrypoint: Annotated[
        StrictStr, Field(max_length=200), _matches(_MODULE_RE, 'entrypoint must be a Python module name')
    ] = DEFAULT_ENTRYPOINT


class DatasetPrefix(_Strict):
    bucket: Bucket
    prefix: Prefix


class DatasetObject(_Strict):
    bucket: Bucket
    key: ObjectKey


def _max_config_bytes(info: ValidationInfo) -> int:
    return (info.context or {}).get('max_config_bytes', 256 * 1024)


class JsonConfig(_Strict):
    format: Literal['json']
    value: dict[str, Any]

    @model_validator(mode='after')
    def _size(self, info: ValidationInfo) -> JsonConfig:
        limit = _max_config_bytes(info)
        if len(json.dumps(self.value, separators=(',', ':'), ensure_ascii=False).encode()) > limit:
            raise ValueError(f'config exceeds {limit} bytes')
        return self


class YamlConfig(_Strict):
    format: Literal['yaml']
    value: StrictStr

    @model_validator(mode='after')
    def _mapping(self, info: ValidationInfo) -> YamlConfig:
        limit = _max_config_bytes(info)
        if len(self.value) > limit:
            raise ValueError(f'config exceeds {limit} bytes')
        try:
            doc = yaml.safe_load(self.value)
        except yaml.YAMLError as err:
            raise ValueError(f'invalid YAML: {err}') from None
        if not isinstance(doc, dict):
            raise ValueError('YAML config must be a mapping')
        return self


class ObjectConfig(_Strict):
    object: ObjectRef


class Compute(_Strict):
    gpuType: Annotated[StrictStr, Field(min_length=1, max_length=64, pattern=r'^[A-Za-z0-9-]+$')]
    gpuCount: Annotated[StrictInt, Field(ge=1, le=64)]
    maxRuntimeMinutes: Annotated[StrictInt | None, Field(ge=1, exclude_if=lambda v: v is None)] = None


class CreateRun(_Strict):
    name: Name
    trainer: Trainer
    dataset: DatasetPrefix | DatasetObject
    config: JsonConfig | YamlConfig | ObjectConfig
    compute: Compute

    def document(self) -> dict[str, Any]:
        """The provided fields as plain JSON values, ready to merge into a run."""
        return {name: _plain(getattr(self, name)) for name in self.model_fields_set}


class UpdateRun(CreateRun):
    name: Name | None = None
    trainer: Trainer | None = None
    dataset: DatasetPrefix | DatasetObject | None = None
    config: JsonConfig | YamlConfig | ObjectConfig | None = None
    compute: Compute | None = None

    @model_validator(mode='after')
    def _not_empty(self) -> UpdateRun:
        if not self.model_fields_set:
            raise ValueError('at least one field must be provided')
        if nulls := sorted(f for f in self.model_fields_set if getattr(self, f) is None):
            raise ValueError(f'fields must not be null: {", ".join(nulls)}')
        return self


def _plain(value: Any) -> Any:
    return value.model_dump() if isinstance(value, BaseModel) else value


class ListQuery(BaseModel):
    limit: int = Field(50, ge=1, le=200)
    offset: int = Field(0, ge=0)
    mine: Literal['true', 'false'] | None = None


class LogsQuery(BaseModel):
    tail: int = Field(1000, ge=1, le=20000)

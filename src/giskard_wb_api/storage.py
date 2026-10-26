"""MinIO (S3 API) access. All credentials stay in this process; callers only get
object metadata and short-lived presigned GET URLs.

boto3 is synchronous, so every network call runs in a worker thread.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from .config import MinioConfig


@dataclass(frozen=True)
class StoredObject:
    key: str
    size: int
    last_modified: datetime | None = None
    etag: str | None = None


@dataclass(frozen=True)
class ListResult:
    objects: list[StoredObject]
    truncated: bool


class ObjectStorage(Protocol):
    async def object_exists(self, bucket: str, key: str) -> bool: ...

    async def prefix_has_objects(self, bucket: str, prefix: str) -> bool:
        """True if at least one object exists under the prefix."""
        ...

    async def list(self, bucket: str, prefix: str, max_objects: int) -> ListResult: ...

    async def put_object(self, bucket: str, key: str, body: str, content_type: str) -> None: ...

    def presign_get(self, bucket: str, key: str, ttl_seconds: int) -> str: ...


class MinioStorage:
    def __init__(self, config: MinioConfig):
        def client(endpoint: str):
            return boto3.client(
                's3',
                endpoint_url=endpoint,
                region_name=config.region,
                aws_access_key_id=config.access_key,
                aws_secret_access_key=config.secret_key,
                config=BotoConfig(
                    signature_version='s3v4',
                    s3={'addressing_style': 'path' if config.force_path_style else 'auto'},
                ),
            )

        self._client = client(config.endpoint)
        self._presign_client = client(config.public_endpoint)

    def _object_exists(self, bucket: str, key: str) -> bool:
        try:
            self._client.head_object(Bucket=bucket, Key=key)
            return True
        except ClientError as err:
            if err.response.get('ResponseMetadata', {}).get('HTTPStatusCode') == 404:
                return False
            raise

    async def object_exists(self, bucket: str, key: str) -> bool:
        return await asyncio.to_thread(self._object_exists, bucket, key)

    async def prefix_has_objects(self, bucket: str, prefix: str) -> bool:
        out = await asyncio.to_thread(self._client.list_objects_v2, Bucket=bucket, Prefix=prefix, MaxKeys=1)
        return out.get('KeyCount', 0) > 0

    def _list(self, bucket: str, prefix: str, max_objects: int) -> ListResult:
        objects: list[StoredObject] = []
        token: str | None = None
        while True:
            params = {'Bucket': bucket, 'Prefix': prefix, 'MaxKeys': min(1000, max_objects - len(objects))}
            if token:
                params['ContinuationToken'] = token
            out = self._client.list_objects_v2(**params)
            for o in out.get('Contents', []):
                if not o.get('Key'):
                    continue
                etag = o.get('ETag')
                objects.append(
                    StoredObject(o['Key'], o.get('Size', 0), o.get('LastModified'), etag.replace('"', '') if etag else None)
                )
            token = out.get('NextContinuationToken') if out.get('IsTruncated') else None
            if not token or len(objects) >= max_objects:
                return ListResult(objects, token is not None)

    async def list(self, bucket: str, prefix: str, max_objects: int) -> ListResult:
        return await asyncio.to_thread(self._list, bucket, prefix, max_objects)

    async def put_object(self, bucket: str, key: str, body: str, content_type: str) -> None:
        await asyncio.to_thread(
            self._client.put_object, Bucket=bucket, Key=key, Body=body.encode(), ContentType=content_type
        )

    def presign_get(self, bucket: str, key: str, ttl_seconds: int) -> str:
        # Signing is local; no network call.
        return self._presign_client.generate_presigned_url(
            'get_object', Params={'Bucket': bucket, 'Key': key}, ExpiresIn=ttl_seconds
        )

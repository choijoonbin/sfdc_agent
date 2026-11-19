"""Private per-attempt S3 materialization; local execution never imports boto3."""

import asyncio
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
from time import time

_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_HASH = re.compile(r"^[a-f0-9]{64}$")
_RELEASE = re.compile(r"^nextbss-\d+(?:\.\d+){1,2}-[a-f0-9]{64}$")
_FIG_BYTES = 100 * 1024 * 1024
_DESIGN_SOURCE_BYTES = 256 * 1024 * 1024
_RELEASE_BYTES = 128 * 1024 * 1024
_MANIFEST_BYTES = 4 * 1024 * 1024
_CHUNK = 64 * 1024


def _invalid():
    return ValueError("MANAGED_OBJECT_STORAGE_INVALID")


def _key(value):
    if not isinstance(value, str) or len(value.encode()) > 1024 or not value:
        raise _invalid()
    if any(not _PART.fullmatch(part) or part in {".", ".."} for part in value.split("/")):
        raise _invalid()
    return value


def _digest(value):
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise _invalid()
    return value


def _integer(value, maximum):
    if type(value) is not int or not 0 <= value <= maximum:
        raise _invalid()
    return value


def _json(bytes_):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise _invalid()
            result[key] = value
        return result
    value = json.loads(bytes_, object_pairs_hook=unique, parse_constant=lambda value: (_ for _ in ()).throw(_invalid()))
    if not isinstance(value, dict):
        raise _invalid()
    return value


def _private_parent(path):
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if path.parent.resolve(strict=True) != path.parent or path.exists() or path.is_symlink():
        raise _invalid()


def _local_bytes(path, maximum):
    if path.resolve(strict=True) != path:
        raise _invalid()
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > maximum:
            raise _invalid()
        bytes_ = source.read(maximum + 1)
        after = os.fstat(source.fileno())
        if len(bytes_) != info.st_size or after.st_size != info.st_size or after.st_mtime_ns != info.st_mtime_ns:
            raise _invalid()
    return bytes_


async def storage_io(function, *args, **kwargs):
    """Do not remove a job workspace until its bounded background transfer has stopped."""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except BaseException:
        while not task.done():
            try:
                await asyncio.shield(task)
            except BaseException:
                if task.done():
                    break
        # Retrieve a transfer failure without replacing cancellation's original exception.
        if task.done() and not task.cancelled():
            task.exception()
        raise


class ManagedObjectStorage:
    def __init__(self, client, bucket, prefix):
        if not isinstance(bucket, str) or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
            raise _invalid()
        self.client, self.bucket = client, bucket
        self.prefix = _key(prefix.rstrip("/"))

    @classmethod
    def configured(cls):
        provider = os.environ.get("SFDC_STORAGE_PROVIDER", "local")
        if provider == "local":
            return None
        if provider != "s3":
            raise _invalid()
        try:
            import boto3
            from botocore.config import Config
            region = os.environ["AWS_REGION"]
            if not region.strip():
                raise _invalid()
            client = boto3.client("s3", region_name=region, config=Config(
                connect_timeout=5, read_timeout=30, retries={"max_attempts": 1, "mode": "standard"}))
            return cls(client, os.environ["SFDC_STORAGE_S3_BUCKET"], os.environ["SFDC_STORAGE_S3_PREFIX"])
        except Exception:
            raise ValueError("MANAGED_OBJECT_STORAGE_UNAVAILABLE") from None

    def object_key(self, area, key):
        if area not in {"artifacts", "figma-sources", "releases", "release-ingress"}:
            raise _invalid()
        return self.prefix + "/" + area + "/" + _key(key)

    @staticmethod
    def deadline(deadline):
        if deadline is not None and time() >= deadline:
            raise TimeoutError("MANAGED_OBJECT_TRANSFER_EXPIRED")

    def _response(self, area, key, maximum, deadline):
        self.deadline(deadline)
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self.object_key(area, key))
        except Exception:
            raise ValueError("MANAGED_OBJECT_DOWNLOAD_FAILED") from None
        try:
            _integer(response.get("ContentLength"), maximum)
        except BaseException:
            response["Body"].close()
            raise
        return response

    def read(self, area, key, maximum, expected_sha=None, deadline=None):
        if expected_sha is not None:
            _digest(expected_sha)
        response = self._response(area, key, maximum, deadline)
        body = response["Body"]
        try:
            value = bytearray()
            while chunk := body.read(min(_CHUNK, maximum + 1 - len(value))):
                self.deadline(deadline)
                value.extend(chunk)
                if len(value) > maximum:
                    raise _invalid()
            digest = sha256(value).hexdigest()
            if len(value) != response["ContentLength"] or expected_sha and digest != expected_sha:
                raise _invalid()
            metadata = response.get("Metadata", {}).get("sha256")
            if metadata is not None and metadata != digest:
                raise _invalid()
            return bytes(value)
        except (ValueError, TimeoutError):
            raise
        except Exception:
            raise ValueError("MANAGED_OBJECT_DOWNLOAD_FAILED") from None
        finally:
            body.close()

    def download(self, area, key, path, maximum, expected_sha, expected_size=None, deadline=None):
        _digest(expected_sha)
        response = self._response(area, key, maximum, deadline)
        body = response["Body"]
        temporary = path.with_name(path.name + ".part")
        try:
            if expected_size is not None and response["ContentLength"] != _integer(expected_size, maximum):
                raise _invalid()
            _private_parent(path)
            digest, count = sha256(), 0
            with temporary.open("xb") as target:
                os.chmod(temporary, 0o600)
                while chunk := body.read(_CHUNK):
                    self.deadline(deadline)
                    count += len(chunk)
                    if count > maximum:
                        raise _invalid()
                    digest.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            if count != response["ContentLength"] or digest.hexdigest() != expected_sha:
                raise _invalid()
            metadata = response.get("Metadata", {}).get("sha256")
            if metadata is not None and metadata != expected_sha:
                raise _invalid()
            temporary.replace(path)
        except (ValueError, TimeoutError, OSError):
            raise
        except Exception:
            raise ValueError("MANAGED_OBJECT_DOWNLOAD_FAILED") from None
        finally:
            body.close()
            temporary.unlink(missing_ok=True)

    def prepare(self, scratch, *, archive=None, release_id=None, manifest_sha=None, design_sources=False, deadline=None):
        scratch = Path(scratch).resolve(strict=True)
        environment = {}
        if archive is not None:
            key = "/".join((str(archive.tenantId), str(archive.workspaceId), archive.fileKey, archive.sha256 + ".fig"))
            sources = scratch / "sources"
            self.download("figma-sources", key, sources / key, _FIG_BYTES, archive.sha256, deadline=deadline)
            environment["SFDC_MANAGED_SOURCE_ROOT"] = str(sources)
        if release_id is not None:
            if not _RELEASE.fullmatch(release_id):
                raise _invalid()
            _digest(manifest_sha)
            manifest_bytes = self.read("releases", release_id + "/release.json", _MANIFEST_BYTES, manifest_sha, deadline)
            manifest = _json(manifest_bytes)
            entries = manifest.get("files")
            if manifest.get("releaseId") != release_id or not isinstance(entries, list) or not 1 <= len(entries) <= 1024:
                raise _invalid()
            root = scratch / "releases" / release_id
            total, known = 0, set()
            for entry in entries:
                if not isinstance(entry, dict):
                    raise _invalid()
                key = _key(entry.get("path"))
                size = _integer(entry.get("bytes"), 32 * 1024 * 1024)
                checksum = _digest(entry.get("sha256"))
                if key == "release.json" or key.casefold() in known:
                    raise _invalid()
                known.add(key.casefold())
                total += size
                if total > _RELEASE_BYTES:
                    raise _invalid()
                self.download("releases", release_id + "/" + key, root / key, 32 * 1024 * 1024,
                              checksum, size, deadline)
            _private_parent(root / "release.json")
            (root / "release.json").write_bytes(manifest_bytes)
            os.chmod(root / "release.json", 0o600)
            environment["SFDC_MANAGED_RELEASE_ROOT"] = str(root.parent)
        if design_sources:
            index = _json(self.read("figma-sources", "design-sources/index.json", 64 * 1024, deadline=deadline))
            if set(index) != {"guide", "library"}:
                raise _invalid()
            paths = {}
            for kind, entry in index.items():
                if not isinstance(entry, dict) or set(entry) != {"objectKey", "sha256", "sizeBytes"}:
                    raise _invalid()
                path = scratch / "design-sources" / (kind + ".fig")
                self.download("figma-sources", _key(entry["objectKey"]), path, _DESIGN_SOURCE_BYTES,
                              _digest(entry["sha256"]), entry["sizeBytes"], deadline)
                paths[kind] = str(path)
            index_path = scratch / "design-sources" / "index.json"
            index_path.write_text(json.dumps(paths))
            os.chmod(index_path, 0o600)
            environment["SFDC_MANAGED_DESIGN_SOURCES"] = str(index_path)
        return environment

    def put_immutable(self, area, key, value, deadline=None):
        self.deadline(deadline)
        checksum = sha256(value).hexdigest()
        try:
            self.client.put_object(Bucket=self.bucket, Key=self.object_key(area, key), Body=value,
                                   IfNoneMatch="*", Metadata={"sha256": checksum})
        except Exception as error:
            response = getattr(error, "response", {})
            if response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 412 and response.get("Error", {}).get("Code") not in {"PreconditionFailed", "412"}:
                raise ValueError("MANAGED_OBJECT_UPLOAD_FAILED") from None
            if self.read(area, key, len(value), checksum, deadline) != value:
                raise _invalid()

    def publish_output(self, directory, prefix, marker, expected_sha, *, analysis=False, deadline=None):
        directory = Path(directory).resolve(strict=True)
        _key(prefix)
        marker_bytes = _local_bytes(directory / marker, _MANIFEST_BYTES)
        if sha256(marker_bytes).hexdigest() != _digest(expected_sha):
            raise _invalid()
        manifest = _json(marker_bytes)
        if analysis:
            entries = [manifest["view"], *manifest["assets"]]
            total_limit = 128 * 1024 * 1024
        else:
            entries = manifest.get("files")
            total_limit = 100 * 1024 * 1024
        if not isinstance(entries, list) or not 1 <= len(entries) <= 5000:
            raise _invalid()
        files, total = {}, 0
        for entry in entries:
            if not isinstance(entry, dict):
                raise _invalid()
            key = _key(entry.get("path"))
            size = _integer(entry.get("sizeBytes"), 16 * 1024 * 1024)
            checksum = _digest(entry.get("sha256"))
            if key == marker:
                raise _invalid()
            if key in files:
                if files[key] != (size, checksum):
                    raise _invalid()
                continue
            files[key] = size, checksum
            total += size
            if total > total_limit:
                raise _invalid()
        observed = set()
        for file in directory.rglob("*"):
            metadata = file.lstat()
            if stat.S_ISDIR(metadata.st_mode):
                continue
            relative = file.relative_to(directory).as_posix()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or relative not in {*files, marker}:
                raise _invalid()
            observed.add(relative)
        if observed != {*files, marker}:
            raise _invalid()
        for key, (size, checksum) in files.items():
            self.deadline(deadline)
            value = _local_bytes(directory / key, 16 * 1024 * 1024)
            if len(value) != size or sha256(value).hexdigest() != checksum:
                raise _invalid()
            self.put_immutable("release-ingress", prefix + "/" + key, value, deadline)
        # The immutable, hash-pinned manifest is the publication marker and is always last.
        self.put_immutable("release-ingress", prefix + "/" + marker, marker_bytes, deadline)

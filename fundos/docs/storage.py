"""
Pluggable object-storage connectors (Doc 6 §8) — Gyain pattern with FundOS
additions (download_file, signed_url) and an OCI connector (S3-compatible API
— the S3 connector with an endpoint override). Default backend OCI/Azure-India
for residency; Local for dev. Artefact access uses short-lived signed URLs —
no public buckets.
"""
import abc
import hashlib
import hmac
import logging
import os
import shutil
import time

from django.conf import settings

logger = logging.getLogger(__name__)


class BaseStorageConnector(abc.ABC):
    @abc.abstractmethod
    def upload_file(self, local_path: str, remote_path: str) -> bool: ...

    @abc.abstractmethod
    def delete_file(self, remote_path: str) -> bool: ...

    @abc.abstractmethod
    def download_file(self, remote_path: str, local_path: str) -> bool: ...

    @abc.abstractmethod
    def signed_url(self, remote_path: str, expires_sec: int = 900) -> str: ...

    def upload_bytes(self, data: bytes, remote_path: str) -> bool:
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(data)
            tmp = f.name
        try:
            return self.upload_file(tmp, remote_path)
        finally:
            os.unlink(tmp)


class LocalStorageConnector(BaseStorageConnector):
    """Dev backend — files under FUNDOS_STORAGE_LOCAL_ROOT; signed URLs are
    HMAC-signed paths served by the app."""

    def __init__(self):
        self.root = settings.FUNDOS_STORAGE_LOCAL_ROOT
        os.makedirs(self.root, exist_ok=True)

    def _abs(self, remote_path):
        path = os.path.normpath(os.path.join(self.root, remote_path.lstrip("/")))
        if not path.startswith(os.path.normpath(self.root)):
            raise ValueError("path traversal blocked")
        return path

    def upload_file(self, local_path, remote_path):
        try:
            dest = self._abs(remote_path)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copyfile(local_path, dest)
            return True
        except Exception as e:
            logger.error("STORAGE(local): upload failed: %s", e)
            return False

    def delete_file(self, remote_path):
        try:
            os.unlink(self._abs(remote_path))
            return True
        except FileNotFoundError:
            return True
        except Exception as e:
            logger.error("STORAGE(local): delete failed: %s", e)
            return False

    def download_file(self, remote_path, local_path):
        try:
            shutil.copyfile(self._abs(remote_path), local_path)
            return True
        except Exception as e:
            logger.error("STORAGE(local): download failed: %s", e)
            return False

    def signed_url(self, remote_path, expires_sec=900):
        expires = int(time.time()) + expires_sec
        sig = hmac.new(settings.SECRET_KEY.encode(),
                       f"{remote_path}:{expires}".encode(),
                       hashlib.sha256).hexdigest()[:32]
        return f"/api/v1/files/{remote_path}?expires={expires}&sig={sig}"


class S3StorageConnector(BaseStorageConnector):
    """S3-compatible backend (AWS S3 / OCI Object Storage / MinIO)."""

    def __init__(self, endpoint_url=None):
        import boto3  # optional dependency (Doc 9 environments)
        self.bucket = settings.FUNDOS_STORAGE_BUCKET
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint_url or settings.FUNDOS_STORAGE_ENDPOINT_URL,
            aws_access_key_id=os.environ.get("FUNDOS_STORAGE_ACCESS_KEY"),
            aws_secret_access_key=os.environ.get("FUNDOS_STORAGE_SECRET_KEY"),
        )

    def upload_file(self, local_path, remote_path):
        try:
            self.client.upload_file(local_path, self.bucket, remote_path)
            return True
        except Exception as e:
            logger.error("STORAGE(s3): upload failed: %s", e)
            return False

    def delete_file(self, remote_path):
        try:
            self.client.delete_object(Bucket=self.bucket, Key=remote_path)
            return True
        except Exception as e:
            logger.error("STORAGE(s3): delete failed: %s", e)
            return False

    def download_file(self, remote_path, local_path):
        try:
            self.client.download_file(self.bucket, remote_path, local_path)
            return True
        except Exception as e:
            logger.error("STORAGE(s3): download failed: %s", e)
            return False

    def signed_url(self, remote_path, expires_sec=900):
        return self.client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": remote_path},
            ExpiresIn=expires_sec)


class OciStorageConnector(S3StorageConnector):
    """OCI Object Storage (India region) via its S3-compatible endpoint —
    subclass of the S3 connector with the endpoint override (Doc 6 §8)."""

    def __init__(self):
        super().__init__(endpoint_url=settings.FUNDOS_STORAGE_ENDPOINT_URL)


def get_storage() -> BaseStorageConnector:
    backend = getattr(settings, "FUNDOS_STORAGE_BACKEND", "local")
    if backend in ("s3",):
        return S3StorageConnector()
    if backend in ("oci", "azure"):   # azure via S3-compat gateway or SDK later
        return OciStorageConnector()
    return LocalStorageConnector()

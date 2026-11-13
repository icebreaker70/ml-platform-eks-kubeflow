"""데이터셋을 tar.gz 하나로 묶어 S3(또는 로컬 경로)에 올리고 내려받는다."""

from __future__ import annotations

import os
import shutil
import tarfile
import tempfile
from urllib.parse import urlparse


def _is_s3(uri: str) -> bool:
    return uri.startswith("s3://")


def _split(uri: str) -> tuple[str, str]:
    u = urlparse(uri)
    return u.netloc, u.path.lstrip("/")


def upload_dir_as_tar(local_dir: str, uri: str) -> str:
    """local_dir 내용을 tar.gz로 묶어 uri(s3://.../x.tar.gz 또는 로컬 경로)에 저장한다."""
    with tempfile.TemporaryDirectory() as tmp:
        tar_path = os.path.join(tmp, "data.tar.gz")
        with tarfile.open(tar_path, "w:gz") as tar:
            tar.add(local_dir, arcname=".")
        if _is_s3(uri):
            import boto3

            bucket, key = _split(uri)
            boto3.client("s3").upload_file(tar_path, bucket, key)
        else:
            os.makedirs(os.path.dirname(os.path.abspath(uri)), exist_ok=True)
            shutil.copy(tar_path, uri)
    return uri


def download_tar(uri: str, dest_dir: str) -> str:
    """uri의 tar.gz를 dest_dir에 풀고 dest_dir를 반환한다."""
    os.makedirs(dest_dir, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tar_path = os.path.join(tmp, "data.tar.gz")
        if _is_s3(uri):
            import boto3

            bucket, key = _split(uri)
            boto3.client("s3").download_file(bucket, key, tar_path)
        else:
            shutil.copy(uri, tar_path)
        with tarfile.open(tar_path, "r:gz") as tar:
            tar.extractall(dest_dir, filter="data")
    return dest_dir

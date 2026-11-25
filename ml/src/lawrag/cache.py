"""Hugging Face 모델 → S3 캐시 (make llm-cache가 클러스터 Job으로 실행).

노드가 새로 뜰 때마다 Hugging Face에서 받지 않도록, 가중치를 S3 models/<이름>/ 에 한 번 복사해 둔다.
서빙 파드는 init container(aws s3 sync)로 같은 리전 S3에서 받는다.

    python -m lawrag.cache --repo Qwen/Qwen3-8B-FP8 --dest s3://<bucket>/models/qwen3-8b-fp8
    python -m lawrag.cache --repo BAAI/bge-m3 --dest s3://<bucket>/models/bge-m3 --ignore "onnx/*" "imgs/*" "*.jpg"

dest/_COMPLETE.json(리비전 기록)이 있고 리비전이 같으면 건너뛴다. 업로드 도중 실패하면 표시가 없으므로 다시 실행하면 된다.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from urllib.parse import urlparse

MARKER = "_COMPLETE.json"


def split_s3(uri: str) -> tuple[str, str]:
    u = urlparse(uri)
    if u.scheme != "s3" or not u.netloc:
        raise ValueError(f"s3://버킷/경로 형식이 아닙니다: {uri}")
    return u.netloc, u.path.strip("/")


def read_marker(s3, bucket: str, prefix: str) -> dict | None:
    try:
        obj = s3.get_object(Bucket=bucket, Key=f"{prefix}/{MARKER}")
    except s3.exceptions.NoSuchKey:
        return None
    return json.loads(obj["Body"].read())


def cache(repo: str, dest: str, revision: str | None = None, ignore: list[str] | None = None,
          force: bool = False) -> dict:
    import boto3
    from huggingface_hub import HfApi, snapshot_download

    bucket, prefix = split_s3(dest)
    s3 = boto3.client("s3")
    info = HfApi().model_info(repo, revision=revision)
    sha = info.sha
    done = read_marker(s3, bucket, prefix)
    if done and done.get("sha") == sha and not force:
        print(f"[cache] 이미 있음: {dest} ({repo}@{sha[:12]})", flush=True)
        return done

    t0 = time.time()
    with tempfile.TemporaryDirectory(dir=os.environ.get("CACHE_TMP")) as tmp:
        local = snapshot_download(repo, revision=sha, local_dir=tmp, ignore_patterns=ignore or None)
        t1 = time.time()
        files, total = [], 0
        for root, _, names in os.walk(local):
            for n in names:
                path = os.path.join(root, n)
                rel = os.path.relpath(path, local)
                if rel.startswith(".cache"):          # huggingface_hub 메타데이터
                    continue
                s3.upload_file(path, bucket, f"{prefix}/{rel}")
                size = os.path.getsize(path)
                files.append(rel)
                total += size
                print(f"[cache] ↑ {rel} ({size / 1e6:.1f} MB)", flush=True)
    marker = {"repo": repo, "sha": sha, "files": sorted(files), "bytes": total,
              "download_s": round(t1 - t0, 1), "upload_s": round(time.time() - t1, 1)}
    s3.put_object(Bucket=bucket, Key=f"{prefix}/{MARKER}", Body=json.dumps(marker, indent=2).encode())
    print(f"[cache] 완료: {dest} — {len(files)}개 {total / 1e9:.2f} GB, "
          f"받기 {marker['download_s']}초 · 올리기 {marker['upload_s']}초", flush=True)
    return marker


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--repo", required=True)
    p.add_argument("--dest", required=True)
    p.add_argument("--revision", default=None)
    p.add_argument("--ignore", nargs="*", default=None)
    p.add_argument("--force", action="store_true")
    a = p.parse_args(argv)
    cache(a.repo, a.dest, a.revision, a.ignore, a.force)


if __name__ == "__main__":
    main()

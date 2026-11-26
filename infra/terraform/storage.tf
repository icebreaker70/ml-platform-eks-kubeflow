# ---------------------------------------------------------------------------
# S3: 데이터셋(datasets/), MLflow 아티팩트(mlflow/)
# ---------------------------------------------------------------------------
resource "aws_s3_bucket" "artifacts" {
  bucket        = "${var.cluster_name}-${data.aws_caller_identity.current.account_id}-${var.region}"
  force_destroy = var.force_destroy_storage
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# ---------------------------------------------------------------------------
# ECR
#  - idcard-ml         : 데이터 생성/학습/평가 컴포넌트 이미지 (CUDA)
#  - idcard-builder    : MLflow 등록, Bento 빌드, 이미지 빌드, KServe 배포 컴포넌트 이미지
#  - mlflow-server     : MLflow 서버 이미지 (psycopg2, boto3 포함)
#  - idcard-recognizer : 파이프라인이 빌드하는 Bento 서빙 이미지
#  - rag               : 6주차 rag-api · 색인 파이프라인 공용 이미지 (CPU torch, bge-m3)
# ---------------------------------------------------------------------------
locals {
  ecr_repositories = ["idcard-ml", "idcard-builder", "mlflow-server", "idcard-recognizer", "rag"]
}

resource "aws_ecr_repository" "this" {
  for_each = toset(local.ecr_repositories)

  name                 = "${var.cluster_name}/${each.key}"
  image_tag_mutability = "MUTABLE"
  force_delete         = var.force_destroy_storage

  image_scanning_configuration {
    scan_on_push = true
  }
}

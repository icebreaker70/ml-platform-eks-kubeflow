# ---------------------------------------------------------------------------
# EKS Pod Identity: 파드별 IAM 권한 (정적 액세스 키 사용 안 함)
# ---------------------------------------------------------------------------

module "ebs_csi_pod_identity" {
  source  = "terraform-aws-modules/eks-pod-identity/aws"
  version = "2.10.0"

  name                      = "${var.cluster_name}-ebs-csi"
  attach_aws_ebs_csi_policy = true
  # association은 EKS 애드온(aws-ebs-csi-driver)에서 생성한다.
}

module "aws_lb_controller_pod_identity" {
  source  = "terraform-aws-modules/eks-pod-identity/aws"
  version = "2.10.0"

  name                            = "${var.cluster_name}-aws-lbc"
  attach_aws_lb_controller_policy = true

  associations = {
    this = {
      cluster_name    = module.eks.cluster_name
      namespace       = "kube-system"
      service_account = "aws-load-balancer-controller"
    }
  }
}

# MLflow 서버: S3 mlflow/ 접두사 읽기/쓰기 (클라이언트는 --serve-artifacts 프록시를 사용)
module "mlflow_pod_identity" {
  source  = "terraform-aws-modules/eks-pod-identity/aws"
  version = "2.10.0"

  name                 = "${var.cluster_name}-mlflow"
  attach_custom_policy = true
  policy_statements = [
    {
      sid       = "ListBucket"
      actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
      resources = [aws_s3_bucket.artifacts.arn]
    },
    {
      sid       = "ObjectRW"
      actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
      resources = ["${aws_s3_bucket.artifacts.arn}/mlflow/*"]
    },
  ]

  associations = {
    this = {
      cluster_name    = module.eks.cluster_name
      namespace       = "mlflow"
      service_account = "mlflow"
    }
  }
}

# 파이프라인 파드 (프로필 네임스페이스의 default-editor SA):
#  - S3 datasets/ 읽기/쓰기
#  - ECR idcard-recognizer 푸시 (BuildKit에 전달할 인증 토큰 발급)
module "pipeline_pod_identity" {
  source  = "terraform-aws-modules/eks-pod-identity/aws"
  version = "2.10.0"

  name                 = "${var.cluster_name}-pipeline"
  attach_custom_policy = true
  policy_statements = [
    {
      sid       = "ListBucket"
      actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
      resources = [aws_s3_bucket.artifacts.arn]
    },
    {
      sid       = "DatasetRW"
      actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
      resources = ["${aws_s3_bucket.artifacts.arn}/datasets/*"]
    },
    {
      sid       = "EcrAuth"
      actions   = ["ecr:GetAuthorizationToken"]
      resources = ["*"]
    },
    {
      sid = "EcrPush"
      actions = [
        "ecr:BatchCheckLayerAvailability",
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer",
        "ecr:InitiateLayerUpload",
        "ecr:UploadLayerPart",
        "ecr:CompleteLayerUpload",
        "ecr:PutImage",
        "ecr:DescribeImages",
      ]
      resources = [aws_ecr_repository.this["idcard-recognizer"].arn]
    },
  ]

  associations = {
    this = {
      cluster_name    = module.eks.cluster_name
      namespace       = var.kubeflow_profile_namespace
      service_account = "default-editor"
    }
  }
}

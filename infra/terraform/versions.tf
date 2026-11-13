terraform {
  # 검증 버전: Terraform CLI 1.17.x
  required_version = ">= 1.10.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.59" # 검증: 6.73.x (EKS 모듈 v21.29.0 요구사항 >= 6.59)
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 3.3"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 3.4"
    }
  }
}

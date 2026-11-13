output "region" {
  value = var.region
}

output "account_id" {
  value = data.aws_caller_identity.current.account_id
}

output "cluster_name" {
  value = module.eks.cluster_name
}

output "oidc_issuer_url" {
  description = "Kubeflow oauth2-proxy M2M(ServiceAccount 토큰) 검증용 issuer"
  value       = module.eks.cluster_oidc_issuer_url
}

output "bucket" {
  value = aws_s3_bucket.artifacts.bucket
}

output "ecr_registry" {
  value = "${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.region}.amazonaws.com"
}

output "ecr_repositories" {
  value = { for k, r in aws_ecr_repository.this : k => r.repository_url }
}

output "owner" {
  value = var.owner
}

output "allowed_cidrs" {
  value = join(",", var.allowed_cidrs)
}

output "kubeflow_profile_namespace" {
  value = var.kubeflow_profile_namespace
}

output "availability_zones" {
  value = local.azs
}

output "configure_kubectl" {
  value = "aws eks update-kubeconfig --region ${var.region} --name ${module.eks.cluster_name}"
}

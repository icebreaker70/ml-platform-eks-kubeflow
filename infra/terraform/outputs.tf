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

# --- Karpenter (platform/karpenter/apply.sh가 읽는다) ---
output "karpenter_enabled" {
  value = var.enable_karpenter
}

output "karpenter_node_role_name" {
  value = var.enable_karpenter ? module.karpenter[0].node_iam_role_name : ""
}

output "karpenter_nodepool_settings" {
  description = "NodePool 템플릿에 넣을 값 (JSON 배열은 YAML에 그대로 쓸 수 있다)"
  value = {
    gpu_capacity_types   = jsonencode(var.gpu_capacity_types)
    gpu_families         = jsonencode(var.gpu_instance_families)
    gpu_limit            = var.gpu_node_limit
    burst_capacity_types = jsonencode(var.burst_capacity_types)
    burst_cpu_limit      = var.burst_cpu_limit
    llm_enabled          = var.enable_llm_serving
    llm_capacity_types   = jsonencode(var.llm_capacity_types)
    llm_instance_types   = jsonencode(var.llm_instance_types)
    llm_gpu_limit        = var.llm_gpu_limit
  }
}

output "configure_kubectl" {
  value = "aws eks update-kubeconfig --region ${var.region} --name ${module.eks.cluster_name}"
}

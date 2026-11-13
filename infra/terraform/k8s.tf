# ---------------------------------------------------------------------------
# 기본 StorageClass (gp3). Kubeflow(MySQL, SeaweedFS), PostgreSQL, BuildKit 캐시 PVC가 사용한다.
# ---------------------------------------------------------------------------
resource "kubernetes_storage_class_v1" "gp3" {
  metadata {
    name = "gp3"
    annotations = {
      "storageclass.kubernetes.io/is-default-class" = "true"
    }
  }

  storage_provisioner    = "ebs.csi.aws.com"
  reclaim_policy         = "Delete"
  volume_binding_mode    = "WaitForFirstConsumer"
  allow_volume_expansion = true

  # PVC로 동적 생성되는 EBS 볼륨에도 Owner/Project 태그를 붙인다.
  parameters = {
    type               = "gp3"
    encrypted          = "true"
    tagSpecification_1 = "Owner=${var.owner}"
    tagSpecification_2 = "Project=${var.cluster_name}"
  }

  depends_on = [module.eks]
}

# ---------------------------------------------------------------------------
# AWS Load Balancer Controller
# ---------------------------------------------------------------------------
resource "helm_release" "aws_lb_controller" {
  name       = "aws-load-balancer-controller"
  repository = "https://aws.github.io/eks-charts"
  chart      = "aws-load-balancer-controller"
  version    = var.aws_lb_controller_chart_version
  namespace  = "kube-system"

  set = [
    { name = "clusterName", value = module.eks.cluster_name },
    { name = "region", value = var.region },
    { name = "vpcId", value = module.vpc.vpc_id },
    { name = "serviceAccount.create", value = "true" },
    { name = "serviceAccount.name", value = "aws-load-balancer-controller" },
    { name = "replicaCount", value = "1" },
    # 컨트롤러가 만드는 ALB, 대상 그룹, 보안 그룹에 붙는 기본 태그
    { name = "defaultTags.Owner", value = var.owner },
    { name = "defaultTags.Project", value = var.cluster_name },
  ]

  depends_on = [
    module.eks,
    module.aws_lb_controller_pod_identity,
  ]
}

# ---------------------------------------------------------------------------
# NVIDIA device plugin (GPU 노드에 nvidia.com/gpu 리소스 노출)
# ---------------------------------------------------------------------------
resource "helm_release" "nvidia_device_plugin" {
  count = var.install_nvidia_device_plugin ? 1 : 0

  name             = "nvidia-device-plugin"
  repository       = "https://nvidia.github.io/k8s-device-plugin"
  chart            = "nvidia-device-plugin"
  version          = var.nvidia_device_plugin_chart_version
  namespace        = "nvidia-device-plugin"
  create_namespace = true

  # 기본 affinity는 NFD 라벨을 요구하므로 제거하고 node-role=gpu 라벨로 배치한다.
  values = [yamlencode({
    affinity = null
    nodeSelector = {
      "node-role" = "gpu"
    }
  })]

  depends_on = [module.eks]
}

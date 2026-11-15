# ---------------------------------------------------------------------------
# Karpenter — GPU 노드를 "필요할 때만, Spot으로" 띄운다.
#
#  - 이 파일: 컨트롤러 IAM(Pod Identity), 노드 IAM 역할, Spot 중단 알림용 SQS/EventBridge, Helm 설치
#  - NodePool / EC2NodeClass: platform/karpenter/nodepools.yaml.tmpl (make infra가 이어서 적용)
#
# 컨트롤러는 관리형 노드그룹(node-role=cpu)에서 돈다. Karpenter가 만든 노드 위에서 돌면
# 그 노드를 스스로 지울 수 없기 때문이다(차트 기본 affinity도 같은 규칙을 갖고 있다).
# ---------------------------------------------------------------------------
module "karpenter" {
  source  = "terraform-aws-modules/eks/aws//modules/karpenter"
  version = "21.29.0"

  count = var.enable_karpenter ? 1 : 0

  cluster_name    = module.eks.cluster_name
  namespace       = "kube-system"
  service_account = "karpenter"

  # EC2NodeClass의 spec.role에 적는 이름이므로 접두사 없이 고정한다.
  node_iam_role_name            = "${var.cluster_name}-karpenter-node"
  node_iam_role_use_name_prefix = false

  tags = local.tags
}

# Public ECR(OCI) 차트는 익명으로도 받을 수 있지만, 로컬에 만료된 토큰이 남아 있으면 403으로 거부된다.
# 그래서 매번 새 토큰을 발급받아 넘긴다. Public ECR 인증은 us-east-1에서만 발급된다.
data "aws_ecrpublic_authorization_token" "karpenter" {
  count  = var.enable_karpenter ? 1 : 0
  region = "us-east-1"
}

resource "helm_release" "karpenter" {
  count = var.enable_karpenter ? 1 : 0

  name                = "karpenter"
  namespace           = "kube-system"
  repository          = "oci://public.ecr.aws/karpenter"
  repository_username = data.aws_ecrpublic_authorization_token.karpenter[0].user_name
  repository_password = data.aws_ecrpublic_authorization_token.karpenter[0].password
  chart               = "karpenter"
  version             = var.karpenter_chart_version

  values = [yamlencode({
    # 일반 노드가 2대뿐이므로 1개만 띄운다 (기본 2개 + 노드 분산 규칙).
    replicas = 1
    nodeSelector = {
      "node-role" = "cpu"
    }
    serviceAccount = {
      name = "karpenter"
    }
    settings = {
      clusterName       = module.eks.cluster_name
      interruptionQueue = module.karpenter[0].queue_name
    }
    controller = {
      resources = {
        requests = { cpu = "250m", memory = "512Mi" }
        limits   = { memory = "1Gi" }
      }
    }
  })]

  # 토큰은 12시간마다 바뀐다. 바뀔 때마다 릴리스를 다시 건드리지 않도록 무시한다.
  lifecycle {
    ignore_changes = [repository_password]
  }

  depends_on = [
    module.eks,
    module.karpenter,
  ]
}

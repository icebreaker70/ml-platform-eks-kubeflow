data "aws_caller_identity" "current" {}

# 두 인스턴스 타입(일반/GPU)을 모두 제공하는 AZ만 골라 서브넷을 만든다.
# (서울 리전에서 g6 계열은 일부 AZ에서만 제공될 수 있음)
data "aws_ec2_instance_type_offerings" "cpu" {
  location_type = "availability-zone"
  filter {
    name   = "instance-type"
    values = [var.cpu_instance_type]
  }
}

data "aws_ec2_instance_type_offerings" "gpu" {
  location_type = "availability-zone"
  filter {
    name   = "instance-type"
    values = [var.gpu_instance_type]
  }
}

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  # 모든 자원 공통 태그 (Owner 필수).
  # provider default_tags로 Terraform이 직접 만드는 자원에 붙고,
  # 모듈 tags 인자로 노드 EC2/EBS/ENI(launch template)에도 전파한다.
  # 컨트롤러가 만드는 자원은 k8s.tf(StorageClass, LBC defaultTags)에서 따로 지정한다.
  tags = {
    Owner     = var.owner
    Project   = var.cluster_name
    ManagedBy = "terraform"
  }

  gpu_azs = sort(tolist(setintersection(
    toset(data.aws_ec2_instance_type_offerings.cpu.locations),
    toset(data.aws_ec2_instance_type_offerings.gpu.locations),
  )))

  # EKS 컨트롤 플레인은 서브넷 2개(서로 다른 AZ)가 필요하다.
  # GPU 제공 AZ를 앞에 두고 나머지 AZ로 채운다.
  azs = slice(distinct(concat(local.gpu_azs, sort(data.aws_availability_zones.available.names))), 0, 2)

  private_subnets = [for i, _ in local.azs : cidrsubnet(var.vpc_cidr, 4, i)]
  public_subnets  = [for i, _ in local.azs : cidrsubnet(var.vpc_cidr, 8, 200 + i)]
}

# ---------------------------------------------------------------------------
# VPC (단일 NAT 게이트웨이로 비용 절감)
# ---------------------------------------------------------------------------
module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "6.7.3"

  name = "${var.cluster_name}-vpc"
  cidr = var.vpc_cidr

  azs             = local.azs
  private_subnets = local.private_subnets
  public_subnets  = local.public_subnets

  enable_nat_gateway   = true
  single_nat_gateway   = true
  enable_dns_hostnames = true

  public_subnet_tags = {
    "kubernetes.io/role/elb" = 1
  }
  private_subnet_tags = {
    "kubernetes.io/role/internal-elb" = 1
    # Karpenter EC2NodeClass가 이 태그로 노드를 띄울 서브넷을 찾는다.
    "karpenter.sh/discovery" = var.cluster_name
  }

  tags = local.tags
}

# ---------------------------------------------------------------------------
# EKS
# ---------------------------------------------------------------------------
module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "21.29.0"

  name               = var.cluster_name
  kubernetes_version = var.kubernetes_version

  endpoint_public_access       = true
  endpoint_private_access      = true
  endpoint_public_access_cidrs = var.allowed_cidrs

  enable_cluster_creator_admin_permissions = true

  # 클러스터·노드그룹·launch template(인스턴스/볼륨/ENI 태그)까지 Owner 태그 전파
  tags = local.tags

  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.private_subnets

  # 모듈 권장 규칙은 컨트롤 플레인→노드 웹훅 포트를 443/4443/6443/8443/9443/10250만 연다.
  # Istio sidecar injector(istiod)는 15017을 쓰므로 추가하지 않으면 istio-injection이 켜진
  # 네임스페이스(knative-serving, kubeflow 등)에서 파드 생성이 웹훅 타임아웃으로 막힌다.
  # Karpenter가 만드는 노드도 관리형 노드와 같은 보안 그룹을 쓴다 (EC2NodeClass가 이 태그로 찾는다).
  # 아래 15017 규칙도 그대로 적용된다.
  node_security_group_tags = {
    "karpenter.sh/discovery" = var.cluster_name
  }

  node_security_group_additional_rules = {
    ingress_cluster_istio_webhook = {
      description                   = "Cluster API to istiod sidecar injector / validation webhook"
      protocol                      = "tcp"
      from_port                     = 15017
      to_port                       = 15017
      type                          = "ingress"
      source_cluster_security_group = true
    }
  }

  addons = {
    vpc-cni = {
      before_compute = true
    }
    eks-pod-identity-agent = {
      before_compute = true
    }
    kube-proxy = {}
    coredns    = {}
    aws-ebs-csi-driver = {
      pod_identity_association = [{
        role_arn        = module.ebs_csi_pod_identity.iam_role_arn
        service_account = "ebs-csi-controller-sa"
      }]
    }
  }

  eks_managed_node_groups = merge({
    # 일반 노드: Istio, Kubeflow, MLflow, KServe 추론 파드, Karpenter 컨트롤러
    cpu = {
      ami_type       = "AL2023_x86_64_STANDARD"
      instance_types = [var.cpu_instance_type]
      # 모듈이 생성 후 desired_size 변경을 무시하므로 min_size로 노드 수를 보장한다.
      min_size     = var.cpu_node_count
      max_size     = max(var.cpu_node_count, 3)
      desired_size = var.cpu_node_count

      labels = {
        "node-role" = "cpu"
      }

      block_device_mappings = {
        xvda = {
          device_name = "/dev/xvda"
          ebs = {
            volume_size           = 100
            volume_type           = "gp3"
            delete_on_termination = true
          }
        }
      }
    }

    }, { for name, group in {
      # 관리형 GPU 노드그룹 — enable_karpenter = false일 때만 만든다 (온디맨드, 항상 켜져 있음, taint 없음).
      # Karpenter를 쓰면 GPU 노드는 platform/karpenter의 NodePool이 학습 때만 Spot으로 띄운다.
      gpu = {
        ami_type       = "AL2023_x86_64_NVIDIA"
        instance_types = [var.gpu_instance_type]
        min_size       = var.gpu_node_count
        max_size       = max(var.gpu_node_count, 1)
        desired_size   = var.gpu_node_count
        subnet_ids     = [for i, az in local.azs : module.vpc.private_subnets[i] if contains(local.gpu_azs, az)]

        labels = {
          "node-role" = "gpu"
        }

        block_device_mappings = {
          xvda = {
            device_name = "/dev/xvda"
            ebs = {
              volume_size           = 150
              volume_type           = "gp3"
              delete_on_termination = true
            }
          }
        }
      }
  } : name => group if !var.enable_karpenter })
}

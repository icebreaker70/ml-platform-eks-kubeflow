variable "region" {
  description = "AWS 리전"
  type        = string
  default     = "ap-northeast-2"
}

variable "cluster_name" {
  description = "EKS 클러스터 이름 (S3/ECR 이름 접두사로도 사용)"
  type        = string
  default     = "mlops"
}

variable "kubernetes_version" {
  description = "EKS Kubernetes 버전 (Kubeflow 26.10.0 / 26.03.1 모두 1.36 검증)"
  type        = string
  default     = "1.36"
}

variable "vpc_cidr" {
  type    = string
  default = "10.10.0.0/16"
}

variable "allowed_cidrs" {
  description = "EKS API 엔드포인트와 ALB에 접속을 허용할 공인 IP 대역"
  type        = list(string)
  default     = ["180.224.237.157/32"]
}

variable "owner" {
  description = "모든 AWS 자원에 붙이는 Owner 태그 값 (필수)"
  type        = string
  default     = "sejkim"

  validation {
    condition     = length(trimspace(var.owner)) > 0
    error_message = "owner 태그 값은 비워 둘 수 없습니다."
  }
}

# ---------------------------------------------------------------------------
# 노드 그룹
# ---------------------------------------------------------------------------
variable "cpu_instance_type" {
  description = "일반 노드 인스턴스 타입 (리소스가 부족하면 m6i.2xlarge로 상향)"
  type        = string
  default     = "m6i.xlarge"
}

variable "cpu_node_count" {
  description = "일반 노드 수 (Kubeflow·MLflow·KServe 추론 파드를 나눠 싣기 위해 2대)"
  type        = number
  default     = 2
}

variable "gpu_instance_type" {
  description = "GPU 인스턴스 타입 (NVIDIA L4). 서브넷 AZ 선택 기준이며, enable_karpenter = false일 때는 관리형 GPU 노드그룹의 타입"
  type        = string
  default     = "g6.xlarge"
}

variable "gpu_node_count" {
  description = "관리형 GPU 노드 수 (enable_karpenter = false일 때만 사용). 플랫폼 파드도 함께 배치되므로 0으로 줄이지 않는다"
  type        = number
  default     = 1
}

# ---------------------------------------------------------------------------
# Karpenter (GPU 노드를 학습 때만 Spot으로)
# ---------------------------------------------------------------------------
variable "enable_karpenter" {
  description = "true면 관리형 GPU 노드그룹 대신 Karpenter NodePool이 GPU 노드를 필요할 때만 띄운다"
  type        = bool
  default     = true
}

variable "karpenter_chart_version" {
  description = "Karpenter Helm 차트 버전 (Kubernetes 1.36은 1.13 이상 필요)"
  type        = string
  default     = "1.15.1"
}

variable "gpu_capacity_types" {
  description = "GPU 노드 구매 방식. 둘 다 적으면 Spot을 먼저 시도하고, 용량이 없을 때 온디맨드로 넘어간다. Spot만 쓰려면 [\"spot\"]"
  type        = list(string)
  default     = ["spot", "on-demand"]

  validation {
    condition     = length(var.gpu_capacity_types) > 0 && alltrue([for t in var.gpu_capacity_types : contains(["spot", "on-demand"], t)])
    error_message = "gpu_capacity_types에는 \"spot\", \"on-demand\"만 쓸 수 있습니다."
  }
}

variable "gpu_instance_families" {
  description = "Karpenter가 고를 수 있는 GPU 인스턴스 계열. 여러 계열을 허용할수록 Spot을 구하기 쉽고 싸다 (g6=L4, g5=A10G, g4dn=T4)"
  type        = list(string)
  default     = ["g6", "g5", "g4dn"]
}

variable "gpu_node_limit" {
  description = "Karpenter가 동시에 띄울 수 있는 GPU 개수 상한 (과금 폭주 방지)"
  type        = number
  default     = 1
}

variable "burst_capacity_types" {
  description = "파이프라인 단계용 임시 CPU 노드(burst)의 구매 방식"
  type        = list(string)
  default     = ["spot", "on-demand"]

  validation {
    condition     = length(var.burst_capacity_types) > 0 && alltrue([for t in var.burst_capacity_types : contains(["spot", "on-demand"], t)])
    error_message = "burst_capacity_types에는 \"spot\", \"on-demand\"만 쓸 수 있습니다."
  }
}

variable "burst_cpu_limit" {
  description = "임시 CPU 노드의 vCPU 합계 상한. 0이면 burst NodePool을 만들지 않는다"
  type        = number
  default     = 8
}

# ---------------------------------------------------------------------------
# 플랫폼
# ---------------------------------------------------------------------------
variable "kubeflow_profile_namespace" {
  description = "파이프라인이 실행되는 Kubeflow 사용자 프로필 네임스페이스"
  type        = string
  default     = "kubeflow-user-example-com"
}

variable "aws_lb_controller_chart_version" {
  description = "AWS Load Balancer Controller Helm 차트 버전"
  type        = string
  default     = "3.6.2"
}

variable "install_nvidia_device_plugin" {
  description = "NVIDIA device plugin 설치 여부 (GPU AMI에 이미 포함된 경우 false)"
  type        = bool
  default     = true
}

variable "nvidia_device_plugin_chart_version" {
  type    = string
  default = "0.20.1"
}

variable "force_destroy_storage" {
  description = "terraform destroy 시 S3 버킷/ECR 이미지를 함께 삭제할지 여부"
  type        = bool
  default     = true
}

"""신분증 인식 학습·배포 파이프라인 (KFP v2).

data-gen → validate → train(GPU) → evaluate ─(mAP@0.5 ≥ 기준)→ register → bento-build
→ image-build(BuildKit→ECR) → deploy(KServe, 카나리) → smoke-test → promote
"""

from kfp import dsl, kubernetes

from components import (
    bento_build,
    data_gen,
    deploy_kserve,
    evaluate,
    image_build,
    promote,
    register_model,
    smoke_test,
    train,
    validate_data,
)


def _cpu(task, cpu: str = "500m", mem: str = "1Gi", mem_limit: str = "4Gi"):
    task.set_cpu_request(cpu).set_memory_request(mem).set_memory_limit(mem_limit)
    # 파이프라인 파드에는 Istio 사이드카를 넣지 않는다 (Job 종료 문제 방지)
    kubernetes.add_pod_annotation(task, "sidecar.istio.io/inject", "false")
    # 같은 태그로 이미지를 다시 푸시해도 노드 캐시(IfNotPresent)가 아니라 새 이미지를 쓰도록 한다.
    # 레이어는 캐시를 그대로 쓰고 매니페스트 다이제스트만 확인하므로 추가 비용이 거의 없다.
    kubernetes.set_image_pull_policy(task, "Always")
    # 일반 노드에 자리가 없으면 Karpenter의 임시 CPU 노드(NodePool burst, taint workload=batch)로 갈 수 있게 한다.
    # 자리가 있으면 그대로 일반 노드에서 돌고, Karpenter를 쓰지 않는 구성에서는 아무 영향이 없다.
    kubernetes.add_toleration(task, key="workload", operator="Equal", value="batch", effect="NoSchedule")
    return task


@dsl.pipeline(name="idcard-recognition", description="합성 신분증 검출 모델 학습 → MLflow → BentoML → KServe")
def idcard_pipeline(
    bucket: str,
    image_repo: str,
    region: str = "ap-northeast-2",
    mlflow_uri: str = "http://mlflow.mlflow.svc.cluster.local:5000/mlflow",
    dataset_version: str = "v1",
    num_train: int = 3000,
    num_val: int = 500,
    seed: int = 42,
    epochs: int = 10,
    batch_size: int = 8,
    lr: float = 0.01,
    map50_threshold: float = 0.6,
    model_name: str = "idcard-detector",
    isvc_name: str = "idcard-recognizer",
    canary_percent: int = 10,
    buildkit_addr: str = "tcp://buildkitd.buildkit.svc.cluster.local:1234",
    gateway_url: str = "http://istio-ingressgateway.istio-system.svc.cluster.local",
):
    gen = _cpu(data_gen(bucket=bucket, dataset_version=dataset_version, num_train=num_train,
                        num_val=num_val, seed=seed), cpu="1", mem="2Gi")
    gen.set_caching_options(True)

    val = _cpu(validate_data(dataset_uri=gen.output))

    tr = train(dataset_uri=gen.output, mlflow_uri=mlflow_uri, epochs=epochs, batch_size=batch_size, lr=lr)
    # CPU 요청은 1코어로 둔다. 관리형 GPU 노드(taint 없음)를 쓰는 구성에서는 플랫폼·추론 파드와 CPU를 나눠 써서
    # 2코어 요청이 Pending이 됐다(EKS 실측 20분 대기). CPU limit이 없으므로 학습 중에는 남는 코어를 그대로 쓴다.
    _cpu(tr, cpu="1", mem="6Gi", mem_limit="12Gi")
    tr.after(val)
    tr.set_caching_options(False)
    tr.set_accelerator_type("nvidia.com/gpu").set_accelerator_limit(1)
    # node-role=gpu 노드에만 배치한다. Karpenter 구성에서는 이 파드가 Pending이 되는 순간 GPU 노드(Spot 우선)가 뜨고,
    # 그 노드에는 nvidia.com/gpu taint가 걸려 있어 아래 toleration을 가진 파드만 올라간다.
    kubernetes.add_node_selector(tr, "node-role", "gpu")
    kubernetes.add_toleration(tr, key="nvidia.com/gpu", operator="Exists", effect="NoSchedule")
    # Spot 노드는 2분 전 통보만 하고 회수될 수 있다. 회수되면 학습을 처음부터 한 번 더 시도한다.
    tr.set_retry(num_retries=1)
    # DataLoader 워커용 공유 메모리
    kubernetes.empty_dir_mount(tr, volume_name="dshm", mount_path="/dev/shm", medium="Memory", size_limit="2Gi")

    ev = _cpu(evaluate(dataset_uri=gen.output, mlflow_uri=mlflow_uri, run_id=tr.outputs["run_id"]),
              cpu="2", mem="3Gi", mem_limit="6Gi")
    ev.set_caching_options(False)
    # evaluate도 DataLoader 워커를 쓰므로 /dev/shm(기본 64Mi)을 늘린다. 없으면 shm "No space left on device".
    kubernetes.empty_dir_mount(ev, volume_name="dshm", mount_path="/dev/shm", medium="Memory", size_limit="1Gi")

    with dsl.If(ev.outputs["map50"] >= map50_threshold, name="map50-passed"):
        reg = _cpu(register_model(mlflow_uri=mlflow_uri, run_id=tr.outputs["run_id"],
                                  model_uri=tr.outputs["model_uri"], model_name=model_name,
                                  map50=ev.outputs["map50"]))
        reg.set_caching_options(False)

        bb = _cpu(bento_build(mlflow_uri=mlflow_uri, model_name=model_name, model_version=reg.output),
                  cpu="1", mem="3Gi", mem_limit="6Gi")
        bb.set_caching_options(False)

        ib = _cpu(image_build(bento=bb.outputs["bento"], image_repo=image_repo, region=region,
                              buildkit_addr=buildkit_addr), mem="1Gi", mem_limit="3Gi")
        ib.set_caching_options(False)

        dep = _cpu(deploy_kserve(isvc_name=isvc_name, image=ib.output, model_version=reg.output,
                                 canary_percent=canary_percent))
        dep.set_caching_options(False)

        sm = _cpu(smoke_test(isvc_name=isvc_name, gateway_url=gateway_url, deploy_mode=dep.output))
        sm.set_caching_options(False)

        pr = _cpu(promote(isvc_name=isvc_name, mlflow_uri=mlflow_uri, model_name=model_name,
                          model_version=reg.output))
        pr.after(sm)
        pr.set_caching_options(False)

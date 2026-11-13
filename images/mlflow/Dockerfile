# MLflow 서버 이미지: PostgreSQL 백엔드(psycopg2) + S3 아티팩트(boto3)
ARG MLFLOW_VERSION=3.17.2
FROM python:3.12-slim
ARG MLFLOW_VERSION

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 MLFLOW_DISABLE_AGENT_HINT=1

RUN pip install "mlflow==${MLFLOW_VERSION}" "psycopg2-binary==2.9.*" "boto3==1.44.2" \
 && useradd -m -u 1000 mlflow

USER 1000
EXPOSE 5000
ENTRYPOINT ["mlflow"]
CMD ["server", "--help"]

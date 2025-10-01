import os

DEFAULT_AWS_INSTANCE = "t2.small"
DRY_TERRAFORM_DEPLOYS = True if os.environ.get("DRY_TERRAFORM_DEPLOYS", "true") == "true" else False

AMI_DATA_SNIPPET = """
data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical
  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"]
  }
}
""".strip()

DIND_WRAPPER_LOCALHOST_FAILOVER="""
version: '3.9'
services:
  dind:
    image: docker:27-dind
    privileged: true
    environment:
      - DOCKER_TLS_CERTDIR=
    ports:
      - "8080:8080"
    volumes:
      - ./:/workspace
    command: >-
      sh -lc "dockerd-entrypoint.sh &\n      while ! docker info >/dev/null 2>&1; do sleep 1; done;\n      cd /workspace && ./setup.sh || true;\n      echo 'Bypassing inner compose to enforce external bind';\n      docker build -t app . && docker run -d -p 8080:{internal_port} --name inner-app app sh -lc 'gunicorn app:app -b 0.0.0.0:{internal_port} || gunicorn main:app -b 0.0.0.0:{internal_port} || python3 -m flask --app app run --host 0.0.0.0 --port {internal_port} || python3 -m flask --app main run --host 0.0.0.0 --port {internal_port}';\n      tail -f /dev/null"
"""

DIND_WRAPPER_DEFAULT_FAILOVER="""
version: '3.9'
services:
  dind:
    image: docker:27-dind
    privileged: true
    environment:
      - DOCKER_TLS_CERTDIR=
    ports:
      - "8080:8080"
    volumes:
      - ./:/workspace
    command: >-
      sh -lc "dockerd-entrypoint.sh &\n      while ! docker info >/dev/null 2>&1; do sleep 1; done;\n      cd /workspace && ./setup.sh || true;\n      if [ -f docker-compose.yml ] || [ -f compose.yaml ] || [ -f compose.yml ]; then\n        docker compose up -d --build;\n      else\n        echo 'No inner compose found; attempting to build Dockerfile and run';\n        docker build -t app . && docker run -d -p 8080:{internal_port} --name inner-app app;\n      fi;\n      tail -f /dev/null"
"""

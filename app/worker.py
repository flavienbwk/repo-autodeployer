import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import List, Optional

import logging
from .queue import JobManager
from .openai_client import generate_terraform_from_llm
from .constants import AMI_DATA_SNIPPET

DEFAULT_AWS_INSTANCE = "t2.small"
DRY_TERRAFORM_DEPLOYS = True if os.environ.get("DRY_TERRAFORM_DEPLOYS", "true") == "true" else False

def run(cmd: List[str], cwd: Optional[str], log: logging.Logger):
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in proc.stdout:  # type: ignore
        log.info(line.rstrip())
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)} (code {proc.returncode})")


def clone_repo(repo_url: str, dest: str, log: logging.Logger):
    run(["git", "clone", "--depth", "1", repo_url, dest], cwd=None, log=log)
    # Remove VCS metadata to avoid transferring unnecessary history and credentials
    git_dir = os.path.join(dest, ".git")
    if os.path.exists(git_dir):
        try:
            shutil.rmtree(git_dir)
            log.info("Removed .git directory at %s", git_dir)
        except Exception as e:
            log.warning("Failed to remove .git directory: %s", e)


def list_tree(root: str, max_depth: int = 4) -> List[str]:
    root_path = Path(root)
    items: List[str] = []

    def _walk(p: Path, depth: int):
        if depth > max_depth:
            return
        for entry in sorted(p.iterdir()):
            rel = str(entry.relative_to(root_path))
            items.append(rel + ("/" if entry.is_dir() else ""))
            if entry.is_dir():
                _walk(entry, depth + 1)

    _walk(root_path, 0)
    return items


COMMON_HTTP_HINTS = [
    # Python
    r"from\s+flask\s+import\s+",
    r"from\s+fastapi\s+import\s+",
    r"django\.core",
    r"uvicorn\.run",
    r"app\.run\(",
    # Node
    r"require\(['\"]express['\"]\)",
    r"from\s+express\s+import|from\s+['\"]express['\"]",
    r"app\.listen\(",
    # Go
    r"http\.ListenAndServe\(",
    # Java/Spring
    r"@RestController",
    r"SpringApplication\.run\(",
]


PORT_PATTERNS = [
    r"EXPOSE\s+(\d+)",
    r"ports:\s*\n\s*-\s*['\"]?(\d+):",
    r"port\s*=\s*(\d+)",
    r"listen\(\s*(\d+)\s*\)",
    r"run\([^)]*port\s*=\s*(\d+)",
    r"--port(?:=|\s+)(\d+)",
]


def is_http_service(repo_dir: str) -> bool:
    for root, _, files in os.walk(repo_dir):
        for f in files:
            if f.endswith((".py", ".js", ".ts", ".go", ".java", ".kt", ".rb", ".rs")) or f.lower() in ("dockerfile", "compose.yaml", "docker-compose.yml"):
                try:
                    path = os.path.join(root, f)
                    with open(path, "r", errors="ignore") as fh:
                        content = fh.read()
                    if any(re.search(pat, content) for pat in COMMON_HTTP_HINTS):
                        return True
                except Exception:
                    continue
    return False


def infer_app_port(repo_dir: str, log: logging.Logger) -> int:
    for root, _, files in os.walk(repo_dir):
        for f in files:
            if f.lower() in ("dockerfile", "docker-compose.yml", "compose.yaml", "compose.yml") or f.endswith((".py", ".js", ".ts", ".go")):
                try:
                    with open(os.path.join(root, f), "r", errors="ignore") as fh:
                        content = fh.read()
                    for pat in PORT_PATTERNS:
                        m = re.search(pat, content, flags=re.MULTILINE)
                        if m:
                            port = int(m.group(1))
                            if 1 <= port <= 65535:
                                log.info("Inferred app port %s from %s", port, f)
                                return port
                except Exception:
                    continue
    # Fallbacks by common frameworks
    hints = [
        ("flask", 5000), ("fastapi", 8000), ("django", 8000),
        ("express", 3000), ("next", 3000), ("rails", 3000), ("spring", 8080), ("go", 8080)
    ]
    for name, port in hints:
        for root, _, files in os.walk(repo_dir):
            for f in files:
                if f.endswith((".py", ".js", ".ts", ".rb", ".java", ".go")):
                    try:
                        with open(os.path.join(root, f), "r", errors="ignore") as fh:
                            content = fh.read().lower()
                        if name in content:
                            log.info("Fallback inferred by framework %s: port %s", name, port)
                            return port
                    except Exception:
                        continue
    log.info("Could not infer port; defaulting to 8080")
    return 8080


def ensure_docker_assets(repo_dir: str, internal_port: int, log: logging.Logger):
    dockerfile_path = os.path.join(repo_dir, "Dockerfile")
    compose_path = os.path.join(repo_dir, "docker-compose.yml")
    makefile_path = os.path.join(repo_dir, "Makefile")

    # Generic multi-language Dockerfile that tries common patterns
    dockerfile = f"""
# Generated Dockerfile
FROM ubuntu:22.04

RUN apt-get update && apt-get install -y \
    ca-certificates curl git python3 python3-pip nodejs npm \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app

# Heuristics: install for Python/Node if present
RUN if [ -f requirements.txt ]; then pip3 install --no-cache-dir -r requirements.txt; fi
RUN if [ -f package.json ]; then npm ci || npm install; fi

# Build step for Node if applicable (try, ignore failure)
RUN if [ -f package.json ]; then npm run build || true; fi

EXPOSE {internal_port}

# Start commands heuristics
CMD bash -lc ' \
  if [ -f manage.py ]; then python3 manage.py migrate || true; fi; \
  if [ -f app.py ] || [ -f main.py ]; then python3 -m gunicorn -k uvicorn.workers.UvicornWorker app:app --bind 0.0.0.0:{internal_port} || python3 -m uvicorn app:app --host 0.0.0.0 --port {internal_port}; \
  elif [ -f package.json ]; then npm start -- --port {internal_port}; \
  else python3 -m http.server {internal_port}; fi'
""".lstrip()

    compose = f"""
version: '3.9'
services:
  app:
    build: .
    container_name: app
    restart: unless-stopped
    ports:
      - "8080:{internal_port}"
    environment:
      - PORT={internal_port}
    # Optionally override command via environment or current repo conventions
""".lstrip()

    makefile = """
.PHONY: up down logs

up:
	docker compose up -d --build
	docker compose ps

logs:
	docker compose logs -f --tail=100

down:
	docker compose down -v
""".lstrip()

    # If repo already has docker assets, we will rewrite to fit our requirements
    with open(dockerfile_path, "w") as f:
        f.write(dockerfile)
    with open(compose_path, "w") as f:
        f.write(compose)
    with open(makefile_path, "w") as f:
        f.write(makefile)

    log.info("Docker assets written/updated: Dockerfile, docker-compose.yml, Makefile")


def archive_repo(src_dir: str, dest_tar: str):
    with tarfile.open(dest_tar, "w:gz") as tar:
        tar.add(src_dir, arcname="app")


TERRAFORM_HINTS = f"""
- Region: ca-central-1
- Instance type: {DEFAULT_AWS_INSTANCE}
- OS: Ubuntu 24.04 (Noble) official Canonical AMI
- Open ports: 22 (SSH), 8080 (HTTP)
- Use terraform to:
  - Create an SSH key via tls_private_key ONLY; do not use aws_key_pair.
  - Inject the public key into ubuntu's authorized_keys using cloud-init user_data.
  - Provision security group allowing 22 and 8080 from 0.0.0.0/0 (ingress only). Do not manage egress.
  - Launch EC2 with the above SG
  - Copy the prepared project tar.gz to /opt/app.tar.gz using file provisioner
  - remote-exec: install Docker (latest) and docker compose plugin; extract to /opt/app; run 'make up'
  - Ensure the connection uses user 'ubuntu' and the generated private key
  - Output public_ip
- Provider must rely on AWS_* env vars at runtime (do not hardcode keys).\n
"""


def terraform_fallback_main_tf(name_suffix: str) -> str:
    # Fallback Terraform minimizing IAM requirements: no aws_key_pair, no SG egress management
  return f"""
terraform {{
  required_providers {{
    aws = {{
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }}
    tls = {{
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }}
    local = {{
      source  = "hashicorp/local"
      version = "~> 2.0"
    }}
  }}
}}


provider "aws" {{
  region = var.region
}}


variable "region" {{ default = "ca-central-1" }}
variable "az_suffix" {{ default = "a" }}


resource "tls_private_key" "ssh" {{
  algorithm = "RSA"
  rsa_bits  = 4096
}}


resource "local_file" "private_key_pem" {{
  content              = tls_private_key.ssh.private_key_pem
  filename             = "id_rsa"
  file_permission      = "0600"
  directory_permission = "0700"
}}


{AMI_DATA_SNIPPET}


# Networking: VPC with public subnet and Internet access
resource "aws_vpc" "main" {{
  cidr_block           = "10.0.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags = {{ Name = "autodeployer-vpc" }}
}}

resource "aws_internet_gateway" "igw" {{
  vpc_id = aws_vpc.main.id
  tags = {{ Name = "autodeployer-igw" }}
}}

resource "aws_subnet" "public" {{
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.0.1.0/24"
  availability_zone       = "${{var.region}}${{var.az_suffix}}"
  map_public_ip_on_launch = true
  tags = {{ Name = "autodeployer-public" }}
}}

resource "aws_route_table" "public" {{
  vpc_id = aws_vpc.main.id
  # Note: the local route to 10.0.0.0/16 is implicit in AWS and cannot be explicitly created via Terraform.
  route {{
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.igw.id
  }}
  tags = {{ Name = "autodeployer-rt" }}
}}

resource "aws_route_table_association" "public" {{
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}}

resource "aws_security_group" "app" {{
  name_prefix = "autodeployer-sg-"
  description = "Allow SSH and 8080"
  vpc_id      = aws_vpc.main.id

  ingress {{
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }}
  ingress {{
    from_port   = 8080
    to_port     = 8080
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }}
  egress {{
    from_port        = 0
    to_port          = 0
    protocol         = "-1"
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
  }}
}}


resource "aws_instance" "app" {{
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = "{DEFAULT_AWS_INSTANCE}"
  subnet_id               = aws_subnet.public.id
  vpc_security_group_ids  = [aws_security_group.app.id]
  associate_public_ip_address = true
  user_data = <<-EOT
              #cloud-config
              users:
                - name: ubuntu
                  groups:
                    - sudo
                  sudo: "ALL=(ALL) NOPASSWD:ALL"
                  shell: /bin/bash
                  ssh_authorized_keys:
                    - ${{tls_private_key.ssh.public_key_openssh}}
              EOT
  tags = {{ Name = "autodeployer-{name_suffix}" }}
}}


resource "null_resource" "provision" {{
  depends_on = [aws_instance.app]


  connection {{
    type        = "ssh"
    host        = aws_instance.app.public_ip
    user        = "ubuntu"
    private_key = tls_private_key.ssh.private_key_pem
  }}


  provisioner "file" {{
    source      = "app.tar.gz"
    destination = "/home/ubuntu/app.tar.gz"
  }}


  provisioner "remote-exec" {{
    inline = [
      "sudo -n sed -i 's|http://[^ ]*ec2.archive.ubuntu.com/ubuntu|http://archive.ubuntu.com/ubuntu|g' /etc/apt/sources.list || true",
      "true",
      "sudo -n env DEBIAN_FRONTEND=noninteractive apt-get update -o Acquire::ForceIPv4=true -o Acquire::Retries=3 -o Acquire::http::Timeout=30 -y",
      "sudo -n apt-get install -y ca-certificates curl make gnupg lsb-release",
      "sudo -n install -m 0755 -d /etc/apt/keyrings",
      "curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo -n gpg --dearmor -o /etc/apt/keyrings/docker.gpg",
      "echo \\"deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable\\" | sudo -n tee /etc/apt/sources.list.d/docker.list > /dev/null",
      "sudo -n env DEBIAN_FRONTEND=noninteractive apt-get update -o Acquire::ForceIPv4=true -o Acquire::Retries=3 -o Acquire::http::Timeout=30 -y",
      "sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin",
      "sudo -n usermod -aG docker ubuntu || true",
      "sudo -n mkdir -p /opt/app",
      "sudo -n tar -xzf /home/ubuntu/app.tar.gz -C /opt/",
      "cd /opt/app && sudo -n make up",
    ]
  }}
}}


output "public_ip" {{
  value = aws_instance.app.public_ip
}}
""".lstrip()


def extract_code_block(text: str) -> str:
    # Extract first fenced code block
    m = re.search(r"```(?:hcl|terraform|tf)?\n([\s\S]*?)\n```", text)
    if m:
        return m.group(1)
    return text

def is_llm_tf_acceptable(code: str) -> bool:
    code_l = code.lower()
    # Disallow use of aws_key_pair and explicit egress rules
    if "aws_key_pair" in code_l:
        return False
    # Require explicit outbound egress to ensure internet access
    if "egress" not in code_l:
        return False
    # Require tls_private_key and user_data presence
    if "tls_private_key" not in code_l:
        return False
    if "user_data" not in code_l:
        return False
    if DEFAULT_AWS_INSTANCE not in code_l:
        return False
    return True


def process_deploy_request(job_manager: JobManager, job_id: str, description: str, repo_url: str, workdir: str):
    log = job_manager.get_job_logger(job_id)
    log.info("Cloning repository: %s", repo_url)

    repo_dir = os.path.join(workdir, "repo")
    os.makedirs(repo_dir, exist_ok=True)
    clone_repo(repo_url, repo_dir, log)

    tree = list_tree(repo_dir, max_depth=4)
    log.info("Repository tree (max depth 4):\n%s", "\n".join(tree))

    if not is_http_service(repo_dir):
        raise RuntimeError("Denied: repository does not appear to expose an HTTP-accessible server.")

    port = infer_app_port(repo_dir, log)

    # Ensure dockerization per requirements
    ensure_docker_assets(repo_dir, port, log)

    # Prepare archive to transfer
    tar_path = os.path.join(workdir, "app.tar.gz")
    archive_repo(repo_dir, tar_path)
    log.info("Prepared project archive: %s", tar_path)

    # Ask LLM to generate Terraform
    prompt = {
        "objective": "Generate Terraform to deploy a GitHub repo on AWS EC2 and run via Docker compose.",
        "inputs": {
            "description": description,
            "repo_url": repo_url,
            "repo_tree": tree[:500],
            "port": port,
            "tar_name": os.path.basename(tar_path),
            "job_id_short": (job_id.split("-")[0] or job_id)[:8],
        },
        "requirements": TERRAFORM_HINTS,
        "output": "Provide a single main.tf file content in a fenced code block.",
    }

    log.info("Requesting Terraform generation from OpenAI model...")
    llm_resp = None
    try:
        llm_resp = generate_terraform_from_llm(prompt)
        log.info("Received LLM response")
    except Exception as e:
        log.warning("OpenAI call failed, falling back to built-in Terraform template: %s", e)

    terraform_dir = os.path.join(workdir, "terraform")
    os.makedirs(terraform_dir, exist_ok=True)

    main_tf = None
    if llm_resp and isinstance(llm_resp, str):
        code = extract_code_block(llm_resp)
        if code and is_llm_tf_acceptable(code):
            main_tf = code
        else:
            log.info("LLM Terraform rejected by policy; using fallback template")
    if not main_tf:
        short_id = (job_id.split("-")[0] or job_id)[:8]
        main_tf = terraform_fallback_main_tf(short_id)

    with open(os.path.join(terraform_dir, "main.tf"), "w") as f:
        f.write(main_tf)
    # Place archive next to TF for file provisioner
    shutil.copy2(tar_path, os.path.join(terraform_dir, os.path.basename(tar_path)))

    log.info("Executing Terraform init/apply in %s", terraform_dir)

    # Run terraform commands; expect terraform binary to be available in container
    run(["terraform", "init"], cwd=terraform_dir, log=log)
    if DRY_TERRAFORM_DEPLOYS:
      run(["terraform", "plan", "-out=tfplan"], cwd=terraform_dir, log=log)
    else:
      run(["terraform", "apply", "-auto-approve"], cwd=terraform_dir, log=log)

    log.info("Deployment requested. Monitor AWS resources and app at port 8080.")

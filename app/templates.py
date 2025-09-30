# Centralized large text templates with .format-style placeholders
# Use double braces {{ }} to emit literal braces in HCL/YAML where needed

DOCKERFILE_FALLBACK_TEMPLATE = """
# Generated Dockerfile (fallback)
FROM ubuntu:22.04

RUN apt-get update && apt-get install -y \
    ca-certificates curl git python3 python3-pip nodejs npm \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app

# Heuristics: install deps for Python/Node if present (support nested common dirs)
RUN bash -lc 'set -e; \
  if [ -f requirements.txt ]; then \
    pip3 install --no-cache-dir -r requirements.txt; \
  else \
    for d in app src server backend; do \
      if [ -f "$d/requirements.txt" ]; then pip3 install --no-cache-dir -r "$d/requirements.txt"; break; fi; \
    done; \
  fi'
RUN if [ -f package.json ]; then npm ci || npm install; fi

# Build step for Node if applicable (try, ignore failure)
RUN if [ -f package.json ]; then npm run build || true; fi

EXPOSE {internal_port}

# Start commands heuristics (prefer Python HTTP apps; support nested app directories)
CMD bash -lc 'set -e; \
  # Django migrations if a manage.py exists anywhere
  DJANGO_DIR=""; \
  for d in . app src server backend; do \
    if [ -f "$d/manage.py" ]; then DJANGO_DIR="$d"; break; fi; \
  done; \
  if [ -n "$DJANGO_DIR" ]; then \
    cd "$DJANGO_DIR"; python3 manage.py migrate || true; cd - >/dev/null; \
  fi; \
  # Locate Python entrypoint directory
  START_DIR=""; \
  for d in . app src server backend; do \
    if [ -f "$d/app.py" ] || [ -f "$d/main.py" ]; then START_DIR="$d"; break; fi; \
  done; \
  if [ -n "$START_DIR" ]; then \
    cd "$START_DIR"; \
    python3 -m gunicorn -k uvicorn.workers.UvicornWorker app:app --bind 0.0.0.0:{internal_port} \
    || python3 -m gunicorn -k uvicorn.workers.UvicornWorker main:app --bind 0.0.0.0:{internal_port} \
    || python3 -m uvicorn app:app --host 0.0.0.0 --port {internal_port} \
    || python3 -m uvicorn main:app --host 0.0.0.0 --port {internal_port} \
    || python3 app.py \
    || python3 main.py; \
  elif [ -f package.json ]; then \
    npm start -- --port {internal_port}; \
  else \
    python3 -m http.server {internal_port}; \
  fi'
""".lstrip()

COMPOSE_TEMPLATE = """
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

MAKEFILE_TEMPLATE = """
.PHONY: up down logs

up:
	docker compose up -d --build
	docker compose ps

logs:
	docker compose logs -f --tail=100

down:
	docker compose down -v
""".lstrip()

TERRAFORM_HINTS_TEMPLATE = """
- Region: ca-central-1
- Instance type: {instance_type}
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
- Provider must rely on AWS_* env vars at runtime (do not hardcode keys).
""".lstrip()

TERRAFORM_FALLBACK_TEMPLATE = """
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

{ami_data_snippet}

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
  instance_type          = "{instance_type}"
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
      "echo \"deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable\" | sudo -n tee /etc/apt/sources.list.d/docker.list > /dev/null",
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

LLM_TERRAFORM_SYSTEM_PROMPT_TEMPLATE = (
    "You are a Terraform expert. Generate minimal, working Terraform (main.tf) with restricted IAM assumptions: "
    "- Provision t2.small in ca-central-1 on Ubuntu 24.04 (Canonical AMI). Ensure the chosen Availability Zone supports this instance type (e.g., prefer ca-central-1a/b); if creating a subnet, set availability_zone accordingly. "
    "- Open ports 22 and 8080 (ingress) and allow all outbound egress (0.0.0.0/0 and ::/0). "
    "- Create an SSH key via tls_private_key ONLY; do NOT use aws_key_pair. "
    "- In cloud-init user_data, add the public key to the ubuntu user's authorized keys using exact Terraform interpolation ${tls_private_key.ssh.public_key_openssh} (single pair of braces), and ensure passwordless sudo: set groups: [sudo] and sudo: \"ALL=(ALL) NOPASSWD:ALL\" for the ubuntu user. "
    "- For networking, ensure outbound internet: attach the instance to a public subnet with an Internet Gateway and route 0.0.0.0/0 to the IGW. The local route for the VPC CIDR (e.g., 10.0.0.0/16) is implicit in AWS route tables; do not try to create it. If a default VPC/subnet isn’t available, create a minimal VPC + public subnet + IGW + route table + association, and enable DNS support/hostnames on the VPC. "
    "- For Security Groups, DO NOT set a fixed 'name'. Instead set name_prefix = \"autodeployer-sg-\" to avoid duplicate name errors. "
    "- Upload a provided app.tar.gz to /home/ubuntu/app.tar.gz using file provisioner. "
    "- Tag the EC2 instance Name as autodeployer-<job_id_short>, where job_id_short is provided in inputs. "
    "- To avoid mirror/network issues: prefer forcing IPv4 by passing -o Acquire::ForceIPv4=true to apt-get update/installs (instead of writing apt.conf), replace any ec2.archive.ubuntu.com mirrors with archive.ubuntu.com in sources, and add apt retries/timeouts. "
    "- Install Docker + compose, extract to /opt/app, run 'make up'. Prefix all privileged commands with sudo -n and use DEBIAN_FRONTEND=noninteractive with apt-get to avoid prompts. "
    "- Use AWS credentials from env; do not hardcode. "
    "- Avoid any data sources or resources that require ec2:DescribeKeyPairs or RevokeSecurityGroupEgress. "
    "- Use this AMI data block verbatim to look up Canonical Ubuntu 24.04 in any region: \n\n"
    "```hcl\n{ami_data_snippet}\n```\n\n"
    "Reference it as: ami = data.aws_ami.ubuntu.id. Output only a single main.tf in one fenced code block."
)

LLM_DOCKERFILE_SYSTEM_PROMPT = (
    "You are a senior DevOps engineer. Produce a working Dockerfile tailored to the provided project files. "
    "Requirements: "
    "- Choose an appropriate official base image (e.g., python:*-slim for Python, node:* for Node). "
    "- Install dependencies from the correct directory (requirements.txt, pyproject.toml, package.json, etc.), accounting for nested app directories (e.g., app/, src/, server/, backend/). "
    "- Set WORKDIR, COPY only what is needed first for better layer caching if obvious. "
    "- Expose the provided port (ENV PORT may be set, but EXPOSE must match). "
    "- Start the actual HTTP server for the app (prefer gunicorn/uvicorn for FastAPI/ASGI, flask run or gunicorn for Flask, django runserver for Django, npm start for Node, etc.). "
    "- Do not emit docker-compose.yml content; Dockerfile only. "
    "Output format rules (mandatory): Respond with ONLY the Dockerfile content, no Markdown code fences, no surrounding quotes, and no explanations."
)

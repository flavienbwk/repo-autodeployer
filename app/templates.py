# Centralized large text templates with .format-style placeholders
# Use double braces {{ }} to emit literal braces in HCL/YAML where needed

COMPOSE_TEMPLATE = """
version: '3.9'
services:
  app:
    build:
      context: ./repo
      dockerfile: ../Dockerfile
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
	@if [ -f setup.sh ]; then \
		echo "Running setup.sh"; \
		bash ./setup.sh; \
	fi
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
  - Provision security group allowing 22 and 8080 from 0.0.0.0/0 (ingress only). Explicitely allow all egress (ipv4 0.0.0.0/0 and ipv6 ::/0).
  - Launch EC2 with the above SG
  - Copy the prepared project tar.gz to /opt/app.tar.gz using file provisioner
  - remote-exec: install Docker (latest) and docker compose plugin; extract to /opt/app; run 'make up'
  - Ensure the connection uses user 'ubuntu' and the generated private key
  - Output public_ip
- Provider must rely on AWS_* env vars at runtime (do not hardcode keys).
""".lstrip()

REMOTE_EXEC_SNIPPET = """
provisioner "remote-exec" {
  inline = [
    "sudo -n sed -i 's|http://[^ ]*ec2.archive.ubuntu.com/ubuntu|http://archive.ubuntu.com/ubuntu|g' /etc/apt/sources.list || true",
    "sudo -n env DEBIAN_FRONTEND=noninteractive apt-get update -o Acquire::ForceIPv4=true -o Acquire::Retries=3 -o Acquire::http::Timeout=30 -y",
    "sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y make curl",
    "sudo -n env DEBIAN_FRONTEND=noninteractive curl -fsSL https://get.docker.com | sudo sh",
    "sudo -n groupadd -f docker",
    "sudo -n usermod -aG docker ubuntu",
    "sudo -n systemctl enable --now docker || sudo -n service docker start || true",
    "sudo -n mkdir -p /opt",
    "sudo -n tar -xzf /home/ubuntu/app.tar.gz -C /opt/",
    "cd /opt/app && sudo -n -E make up",
  ]
}
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

  {remote_exec_snippet}
}}

output "public_ip" {{
  value = aws_instance.app.public_ip
}}
""".lstrip()

LLM_TERRAFORM_SYSTEM_PROMPT_TEMPLATE = (
    "You are a Terraform expert. Generate minimal, working Terraform (main.tf) with restricted IAM assumptions: "
    "- Provision t2.small in ca-central-1 on Ubuntu 24.04 (Canonical AMI). Ensure the chosen Availability Zone supports this instance type (e.g., prefer ca-central-1a/b); if creating a subnet, set availability_zone accordingly. "
    "- Open ports 22 and 8080 (ingress) and allow all outbound egress (ipv4 0.0.0.0/0 and ipv6 ::/0). "
    "- Create an SSH key via tls_private_key ONLY; do NOT use aws_key_pair. "
    "- In cloud-init user_data, add the public key to the ubuntu user's authorized keys using exact Terraform interpolation ${{tls_private_key.ssh.public_key_openssh}} (single pair of braces), and ensure passwordless sudo: set groups: [sudo] and sudo: \"ALL=(ALL) NOPASSWD:ALL\" for the ubuntu user. "
    "- For networking, ensure outbound internet: attach the instance to a public subnet with an Internet Gateway and route 0.0.0.0/0 to the IGW. The local route for the VPC CIDR (e.g., 10.0.0.0/16) is implicit in AWS route tables; do not try to create it. If a default VPC/subnet isn’t available, create a minimal VPC + public subnet + IGW + route table + association, and enable DNS support/hostnames on the VPC. "
    "- For Security Groups, DO NOT set a fixed 'name'. Instead set name_prefix = \"autodeployer-sg-\" to avoid duplicate name errors. "
    "- Upload a provided app.tar.gz to /home/ubuntu/app.tar.gz using file provisioner. "
    "- Tag the EC2 instance Name as autodeployer-<job_id_short>, where job_id_short is provided in inputs. "
    "- To avoid mirror/network issues: prefer forcing IPv4 by passing -o Acquire::ForceIPv4=true to apt-get update/installs (instead of writing apt.conf), replace any ec2.archive.ubuntu.com mirrors with archive.ubuntu.com in sources, and add apt retries/timeouts. "
    "- Install Docker + compose, extract to /opt/app, run 'make up'. Prefix all privileged commands with sudo -n and use DEBIAN_FRONTEND=noninteractive with apt-get to avoid prompts. "
    "- Use AWS credentials from env; do not hardcode. "
    "- Avoid any data sources or resources that require ec2:DescribeKeyPairs. "
    "- Use this AMI data block verbatim to look up Canonical Ubuntu 24.04 in any region: \n\n"
    "```hcl\n{ami_data_snippet}\n```\n\n"
    "Reference it as: ami = data.aws_ami.ubuntu.id.\n"
    "- Use this remote-exec provisioner block verbatim under your provisioning resource (with a valid SSH connection and a preceding file provisioner that uploads /home/ubuntu/app.tar.gz):\n\n"
    "```hcl\n{remote_exec_snippet}\n```\n"
    "- Output only a single main.tf in one fenced code block. "
    "- When writing inline remote-exec command lists in HCL, ensure any inner double quotes are escaped as \\\" (e.g., echo \\\"...\\\")."
)

LLM_DOCKERFILE_SYSTEM_PROMPT = f"""
    "You are a senior DevOps engineer. Produce a working Dockerfile tailored to the provided project files. "
    "Requirements: "
    "- Choose an appropriate official base image (e.g., python:*\-slim for Python, node:* for Node). "
    "- Install dependencies from the correct directory (requirements.txt, pyproject.toml, package.json, etc.), accounting for nested app directories (e.g., app/, src/, server/, backend/). "
    "- Set WORKDIR, COPY only what is needed first for better layer caching if obvious. "
    "- Expose the provided port (ENV PORT may be set, but EXPOSE must match). "
    "- Regardless of what the source code does, ensure the server binds to 0.0.0.0 so it is reachable from outside the container. If the code uses app.run(host=\"127.0.0.1\", ...), override by invoking a production server (gunicorn for Flask/Wsgi, uvicorn for ASGI) binding to 0.0.0.0. "
    "- Start the actual HTTP server for the app (prefer gunicorn for Flask/Wsgi, uvicorn for ASGI, flask run for simple cases; django runserver for Django; npm start for Node, etc.). "
    "- Do not emit docker-compose.yml content; Dockerfile only. "
    "- Output format rules (mandatory): Respond with ONLY the Dockerfile content, no Markdown code fences, no surrounding quotes, and no explanations."
"""

LLM_COMPOSE_SYSTEM_PROMPT = f"""
    "You are a senior DevOps engineer. Generate a valid docker-compose.yml for running the given repository. "
    "Follow these rules: "
    "- Keep in mind repo files are in ./repo. When choosing paths to build the image in compose, consider the compose file you're creating is 1 level above ./repo (refer to the provided tree as this compose file will be found under '../repo', at root level)."
    "- Here is an example of compose configuration for a simple app: {COMPOSE_TEMPLATE}"
    "- If you didn't decided to use Docker-in-Docker, then don't use or refer /var/run/docker.sock"
    "- Generate a compose file that builds the app from available in repo root, maps host 8080 to the app's internal port, and sets PORT env accordingly. Prefer overriding the default run command to ensure the service binds to 0.0.0.0 even if the source code tries to bind 127.0.0.1. For Python/Flask, prefer `gunicorn module:app -b 0.0.0.0:<port>`. "
    "- Ensure the wrapper compose waits for dockerd to be ready before running inner commands. "
    "- Do NOT use ${{PORT}} or other ${{VAR}} expansions in YAML values (Compose substitutes from host). Use $$PORT to defer to the container shell, or hardcode the numeric port provided in context. "
    "- To help you determine the paths to mount or working dir, consider the file tree. You are creating the compose configuration that will be run at the tree top level, so choose working_dir and paths to mount repo files in concordance."
    "- The output MUST be only the YAML content of docker-compose.yml, no code fences, no comments at top, no explanations. "
"""

LLM_SETUP_SCRIPT_SYSTEM_PROMPT = (
    "You are a senior DevOps engineer. Generate an idempotent bash setup script (setup.sh) to prepare running the repository's service(s). "
    "Use signals from the file tree and README to infer necessary steps: creating .env files with sane defaults, running migrations, building assets, generating keys, etc. "
    "Rules: "
    "- Generate code in this script only if project ABSOLUTELY requires it (instructions in README or presence of .env.example or similar files): set only 'echo \"No setup required\"'."
    "- Shebang: #!/usr/bin/env bash and set -euo pipefail. "
    "- The script MUST be idempotent and safe to run multiple times. "
    "- Don't rely on virtual envs anywhere because we use Docker."
    "- Create missing .env files and populate required variables with best-effort defaults documented in README/package files. "
    "- If the project uses docker compose already, leave its files intact and do not edit them; only prepare inputs. "
    "- Print progress with echo. "
    "- Output MUST be only the script content, no code fences, no explanations. "
)

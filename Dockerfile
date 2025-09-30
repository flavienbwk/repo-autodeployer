# API Dockerfile
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Install system deps (git, curl, unzip) and Terraform
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl unzip ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Install Terraform
ARG TF_VERSION=1.9.5
RUN ARCH=$(dpkg --print-architecture) && \
    curl -fsSL https://releases.hashicorp.com/terraform/${TF_VERSION}/terraform_${TF_VERSION}_linux_${ARCH}.zip -o /tmp/terraform.zip && \
    unzip /tmp/terraform.zip -d /usr/local/bin && \
    chmod +x /usr/local/bin/terraform && \
    terraform -v

WORKDIR /opt
COPY requirements.txt /opt/requirements.txt
RUN pip install -r /opt/requirements.txt

# Optionally include source for non-mounted runs (does not affect dev when mounted, e.g., for prod)
COPY app /opt/app

EXPOSE 8000

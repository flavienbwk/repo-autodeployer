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

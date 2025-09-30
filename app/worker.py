import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import List, Optional

import logging
from .queue import JobManager
from .openai_client import generate_terraform_from_llm, generate_dockerfile_from_llm
from .constants import AMI_DATA_SNIPPET
from .templates import (
    DOCKERFILE_FALLBACK_TEMPLATE,
    COMPOSE_TEMPLATE,
    MAKEFILE_TEMPLATE,
    TERRAFORM_HINTS_TEMPLATE,
    TERRAFORM_FALLBACK_TEMPLATE,
)

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

    def read_file_safe(p: str, max_bytes: int = 20000) -> str:
        try:
            with open(p, "r", errors="ignore") as fh:
                data = fh.read(max_bytes)
            return data
        except Exception:
            return ""

    def collect_relevant_files(root: str) -> List[dict]:
        candidates = [
            "requirements.txt", "pyproject.toml", "Pipfile", "Pipfile.lock",
            "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
            "manage.py", "wsgi.py", "asgi.py",
            "app.py", "main.py", "server.py", "run.py",
            "Procfile", "Dockerfile", "README.md",
        ]
        selected: List[dict] = []
        root_path = Path(root)
        # Prefer exact candidate names anywhere in tree (depth-limited)
        for path in root_path.rglob("*"):
            if len(selected) >= 30:
                break
            if path.is_file() and path.name in candidates:
                rel = str(path.relative_to(root_path))
                selected.append({"path": rel, "content": read_file_safe(str(path))})
        # If no obvious python entry found, include a few .py files that hint HTTP
        hints = ["flask", "fastapi", "django", "uvicorn", "app.run(", "listen("]
        if not any(x["path"].endswith(("app.py", "main.py")) for x in selected):
            for path in root_path.rglob("*.py"):
                if len(selected) >= 40:
                    break
                try:
                    txt = read_file_safe(str(path))
                    low = txt.lower()
                    if any(h in low for h in hints):
                        rel = str(path.relative_to(root_path))
                        selected.append({"path": rel, "content": txt})
                except Exception:
                    continue
        return selected

    # Attempt LLM-designed Dockerfile
    try:
        tree = list_tree(repo_dir, max_depth=4)[:500]
        files = collect_relevant_files(repo_dir)
        llm_ctx = {
            "objective": "Design a correct Dockerfile for the repository to run its HTTP service.",
            "internal_port": internal_port,
            "repo_tree": tree,
            "files": files,
        }
        log.info("Requesting Dockerfile generation from OpenAI model...")
        df_resp = generate_dockerfile_from_llm(llm_ctx)
        dockerfile_llm = extract_code_block(df_resp)
        def acceptable(df: str) -> bool:
            s = df.lower()
            return (
                "from " in s and
                (f"expose {internal_port}" in s or "expose ${port}" in s or "expose ${env:port}" in s) and
                ("cmd" in s or "entrypoint" in s) and
                ("```" not in df) and
                (not df.strip().startswith("```") and not df.strip().endswith("```"))
            )
        if dockerfile_llm and acceptable(dockerfile_llm):
            with open(dockerfile_path, "w") as f:
                f.write(dockerfile_llm.strip() + "\n")
            log.info("Wrote Dockerfile from OpenAI suggestion")
        else:
            raise RuntimeError("LLM Dockerfile rejected by policy")
    except Exception as e:
        log.warning("LLM Dockerfile generation failed (%s). Falling back to generic heuristics.", e)
        # Generic multi-language Dockerfile that tries common patterns (improved Python detection, nested dirs)
        dockerfile = DOCKERFILE_FALLBACK_TEMPLATE.format(internal_port=internal_port)
        with open(dockerfile_path, "w") as f:
            f.write(dockerfile)

    # Compose/Makefile remain deterministic
    compose = COMPOSE_TEMPLATE.format(internal_port=internal_port)
    makefile = MAKEFILE_TEMPLATE

    with open(compose_path, "w") as f:
        f.write(compose)
    with open(makefile_path, "w") as f:
        f.write(makefile)

    log.info("Docker assets written/updated: Dockerfile, docker-compose.yml, Makefile")


def archive_repo(src_dir: str, dest_tar: str):
    with tarfile.open(dest_tar, "w:gz") as tar:
        tar.add(src_dir, arcname="app")


TERRAFORM_HINTS = TERRAFORM_HINTS_TEMPLATE.format(instance_type=DEFAULT_AWS_INSTANCE)


def terraform_fallback_main_tf(name_suffix: str) -> str:
    # Fallback Terraform minimizing IAM requirements: no aws_key_pair, no SG egress management
  return TERRAFORM_FALLBACK_TEMPLATE.format(
    instance_type=DEFAULT_AWS_INSTANCE,
    name_suffix=name_suffix,
    ami_data_snippet=AMI_DATA_SNIPPET,
  )


def extract_code_block(text: str) -> str:
    # Extract the first fenced code block of any language. Fallback: strip fences if present.
    m = re.search(r"```[^\n]*\n([\s\S]*?)\n```", text)
    if m:
        return m.group(1)
    stripped = text.strip()
    lines = stripped.splitlines()
    if len(lines) >= 2 and lines[0].startswith("```") and lines[-1].startswith("```"):
        return "\n".join(lines[1:-1])
    # Also strip surrounding triple quotes if they wrap a fenced block
    if len(lines) >= 4 and lines[0].startswith('"""') and lines[-1].startswith('"""'):
        inner = "\n".join(lines[1:-1]).strip()
        m2 = re.search(r"```[^\n]*\n([\s\S]*?)\n```", inner)
        if m2:
            return m2.group(1)
        return inner
    return stripped

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

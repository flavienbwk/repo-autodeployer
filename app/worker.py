import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import List, Optional

import logging
from .queue import JobManager
from .openai_client import generate_terraform_from_llm, generate_dockerfile_from_llm, generate_compose_from_llm, generate_setup_script_from_llm
from .constants import (
    DEFAULT_AWS_INSTANCE,
    DRY_TERRAFORM_DEPLOYS,
    AMI_DATA_SNIPPET
)
from .templates import (
    DOCKERFILE_FALLBACK_TEMPLATE,
    COMPOSE_TEMPLATE,
    MAKEFILE_TEMPLATE,
    TERRAFORM_HINTS_TEMPLATE,
    TERRAFORM_FALLBACK_TEMPLATE,
)

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
    """
    Ensure dockerization per requirements
    """
    dockerfile_path = os.path.join(repo_dir, "Dockerfile")
    compose_path = os.path.join(repo_dir, "docker-compose.yml")
    makefile_path = os.path.join(repo_dir, "Makefile")
    setup_path = os.path.join(repo_dir, "setup.sh")

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
            "Procfile", "Dockerfile", "README.md", "docker-compose.yml", "compose.yaml", "compose.yml",
        ]
        selected: List[dict] = []
        root_path = Path(root)
        # Prefer exact candidate names anywhere in tree (depth-limited)
        for path in root_path.rglob("*"):
            if len(selected) >= 40:
                break
            if path.is_file() and path.name in candidates:
                rel = str(path.relative_to(root_path))
                selected.append({"path": rel, "content": read_file_safe(str(path))})
        # If no obvious python entry found, include a few .py or js/ts files that hint HTTP
        hints = ["flask", "fastapi", "django", "uvicorn", "app.run(", "listen(", "express", "springapplication.run("]
        if not any(x["path"].endswith(("app.py", "main.py")) for x in selected):
            for glob_pat in ("*.py", "*.js", "*.ts"):
                for path in root_path.rglob(glob_pat):
                    if len(selected) >= 50:
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

    def detect_localhost_binding(root: str) -> bool:
        patterns = [
            r"app\.run\([^)]*host\s*=\s*['\"]127\.0\.0\.1['\"]",
            r"host\s*=\s*['\"]localhost['\"]",
            r"--host=127\.0\.0\.1",
        ]
        try:
            for r, _, files in os.walk(root):
                for f in files:
                    if not f.endswith((".py", ".js", ".ts")):
                        continue
                    with open(os.path.join(r, f), "r", errors="ignore") as fh:
                        txt = fh.read()
                    for pat in patterns:
                        if re.search(pat, txt):
                            return True
        except Exception:
            return False
        return False

    # Discover whether the repo already uses Docker/Compose
    has_existing_docker = False
    for root, _, files in os.walk(repo_dir):
        for f in files:
            name_l = f.lower()
            if name_l in ("dockerfile", "docker-compose.yml", "compose.yaml", "compose.yml"):
                has_existing_docker = True
                break
        if has_existing_docker:
            break

    tree = list_tree(repo_dir, max_depth=4)[:500]
    files = collect_relevant_files(repo_dir)
    binds_localhost = detect_localhost_binding(repo_dir)

    # If the repo is not dockerized yet, synthesize Dockerfile via LLM with fallback
    if not has_existing_docker:
        try:
            llm_ctx = {
                "objective": "Design a correct Dockerfile for the repository to run its HTTP service.",
                "internal_port": internal_port,
                "repo_tree": tree,
                "files": files,
                "localhost_binding_detected": binds_localhost,
                "require_bind_host": "0.0.0.0",
            }
            log.info("Requesting Dockerfile generation from OpenAI model...")
            df_resp = generate_dockerfile_from_llm(llm_ctx)
            dockerfile_llm = extract_code_block(df_resp)
            def acceptable(df: str) -> bool:
                s = df.lower()
                conds = [
                    "from " in s,
                    (f"expose {internal_port}" in s or "expose ${port}" in s or "expose ${env:port}" in s),
                    ("cmd" in s or "entrypoint" in s),
                    ("```" not in df),
                ]
                if binds_localhost:
                    conds.append("0.0.0.0" in s)
                return all(conds)
            if dockerfile_llm and acceptable(dockerfile_llm):
                with open(dockerfile_path, "w") as f:
                    f.write(dockerfile_llm.strip() + "\n")
                log.info("Wrote Dockerfile from OpenAI suggestion")
            else:
                raise RuntimeError("LLM Dockerfile rejected by policy")
        except Exception as e:
            log.warning("LLM Dockerfile generation failed (%s). Falling back to generic heuristics.", e)
            dockerfile = DOCKERFILE_FALLBACK_TEMPLATE.format(internal_port=internal_port)
            with open(dockerfile_path, "w") as f:
                f.write(dockerfile)
    else:
        log.info("Existing Docker/Compose detected in repository; will not overwrite Dockerfile.")
        # Generate setup.sh via LLM to prepare env/config before running compose via DinD
        try:
            setup_ctx = {
                "objective": "Generate an idempotent setup.sh to prepare the app (.env, migrations, keys) before running compose under DinD.",
                "repo_tree": tree,
                "files": files,
                "localhost_binding_detected": binds_localhost,
                "require_bind_host": "0.0.0.0",
            }
            log.info("Requesting setup.sh generation from OpenAI model...")
            setup_resp = generate_setup_script_from_llm(setup_ctx)
            setup_script = extract_code_block(setup_resp) or setup_resp
            if setup_script.strip() == "":
                raise RuntimeError("Empty setup.sh from LLM")
            if "#!/" not in (setup_script.splitlines()[0] if setup_script.splitlines() else ""):
                setup_script = "#!/usr/bin/env bash\nset -euo pipefail\n" + setup_script
            with open(setup_path, "w") as f:
                f.write(setup_script.strip() + "\n")
            try:
                os.chmod(setup_path, 0o755)
            except Exception:
                pass
            log.info("Wrote setup.sh from OpenAI suggestion")
        except Exception as e:
            log.warning("Failed to generate setup.sh from LLM: %s. Writing minimal placeholder.", e)
            with open(setup_path, "w") as f:
                f.write("#!/usr/bin/env bash\nset -euo pipefail\necho 'No setup required.'\n")
            try:
                os.chmod(setup_path, 0o755)
            except Exception:
                pass

    # Compose via LLM for both cases (outer wrapper if dockerized, otherwise simple app service)
    try:
        compose_ctx = {
            "objective": "Generate docker-compose.yml for the repository. Use DinD wrapper if already dockerized; otherwise single service build.",
            "internal_port": internal_port,
            "repo_tree": tree,
            "files": files,
            "dockerized": has_existing_docker,
            "localhost_binding_detected": binds_localhost,
            "require_bind_host": "0.0.0.0",
        }
        log.info("Requesting docker-compose.yml generation from OpenAI model...")
        comp_resp = generate_compose_from_llm(compose_ctx)
        compose_yaml = extract_code_block(comp_resp) or comp_resp
        if "services:" not in compose_yaml:
            raise RuntimeError("LLM compose did not include services block")
        # Avoid Compose-time variable interpolation issues: never keep ${PORT} in YAML
        # Replace ${PORT} with $$PORT for runtime shell expansion, and fix port mappings
        compose_yaml = compose_yaml.replace("${PORT}", "$$PORT").replace("${port}", "$$PORT")
        compose_yaml = compose_yaml.replace("8080:${PORT}", f"8080:{internal_port}").replace("8080:${port}", f"8080:{internal_port}")
        with open(compose_path, "w") as f:
            f.write(compose_yaml.strip() + "\n")
    except Exception as e:
        # As a last resort, fallback to previous deterministic template for non-dockerized repos
        if not has_existing_docker:
            log.warning("LLM compose generation failed (%s). Falling back to minimal template.", e)
            with open(compose_path, "w") as f:
                f.write(COMPOSE_TEMPLATE.format(internal_port=internal_port))
        else:
            # If dockerized, synthesize a generic DinD wrapper compose
            log.warning("LLM compose generation failed for dockerized repo (%s). Writing generic DinD wrapper.", e)
            if binds_localhost:
                dind_wrapper = f"""
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
            else:
                dind_wrapper = f"""
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
            with open(compose_path, "w") as f:
                f.write(dind_wrapper)

    # Write Makefile (updated to run setup.sh if present)
    with open(makefile_path, "w") as f:
        f.write(MAKEFILE_TEMPLATE)

    log.info(
        "Containerization assets written/updated: %s%s autodeploy.compose.yml, Makefile",
        "Dockerfile, " if not has_existing_docker else "",
        "setup.sh, " if has_existing_docker else "",
    )


def apply_repo_rewrites(repo_dir: str, log: logging.Logger) -> None:
    """
    Edit web app code if obvious patterns to replace
    """
    try:
        # Define repo-wide, regex-based rewrites (easy to extend). Applied to ALL text files.
        # For now: normalize any localhost:5000 reference to localhost:8080 so external port mapping works.
        rewrite_patterns: List[tuple[re.Pattern[str], str]] = [
            (re.compile(r"http(s)?:\/\/localhost:[0-9]{1,5}\b"), ""),
        ]

        skipped_dirs = {".git", "node_modules", ".venv", "venv", "dist", "build", "__pycache__", ".terraform"}
        files_changed = 0
        total_replacements = 0

        for root, dirs, files in os.walk(repo_dir):
            # Prune heavy/irrelevant directories in-place
            dirs[:] = [d for d in dirs if d not in skipped_dirs]
            for fname in files:
                fpath = os.path.join(root, fname)
                try:
                    # Skip obvious binaries and large blobs
                    if os.path.getsize(fpath) > 2 * 1024 * 1024:
                        continue
                    with open(fpath, "rb") as fhb:
                        chunk = fhb.read(2048)
                        if b"\x00" in chunk:
                            continue  # binary file
                    with open(fpath, "r", errors="ignore") as fhr:
                        original = fhr.read()
                    updated = original
                    replacements_here = 0
                    for pat, repl in rewrite_patterns:
                        updated, n = pat.subn(repl, updated)
                        replacements_here += n
                    if replacements_here > 0 and updated != original:
                        with open(fpath, "w") as fhw:
                            fhw.write(updated)
                        files_changed += 1
                        total_replacements += replacements_here
                except Exception:
                    # Best-effort; continue
                    continue
        if total_replacements > 0:
            log.info("Applied %d replacements across %d file(s) to normalize localhost ports.", total_replacements, files_changed)
        else:
            log.info("No references found to rewrite.")
    except Exception as e:
        log.warning("Failed applying repo-wide rewrites: %s", e)


def archive_repo(src_dir: str, dest_tar: str):
    with tarfile.open(dest_tar, "w:gz") as tar:
        tar.add(src_dir, arcname="app")


TERRAFORM_HINTS = TERRAFORM_HINTS_TEMPLATE.format(instance_type=DEFAULT_AWS_INSTANCE)


def terraform_fallback_main_tf(name_suffix: str) -> str:
    # Fallback Terraform minimizing IAM requirements: no aws_key_pair, no SG egress management
    tf = TERRAFORM_FALLBACK_TEMPLATE.format(
        instance_type=DEFAULT_AWS_INSTANCE,
        name_suffix=name_suffix,
        ami_data_snippet=AMI_DATA_SNIPPET,
    )
    # Ensure Docker service is started before attempting compose usage
    try:
        tf = re.sub(
            r'(\"cd /opt/app && sudo -n make up\",)',
            '\"sudo -n systemctl enable --now docker\",\n      \\1',
            tf,
        )
    except Exception:
        pass
    return tf


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
    # Require our target instance family to reduce surprises
    if DEFAULT_AWS_INSTANCE not in code_l:
        return False
    # Must upload to /home/ubuntu/app.tar.gz and run make up
    if "/home/ubuntu/app.tar.gz" not in code:
        return False
    if "make up" not in code_l:
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

    ensure_docker_assets(repo_dir, port, log)
    apply_repo_rewrites(repo_dir, log)

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
        "requirements": TERRAFORM_HINTS_TEMPLATE.format(instance_type=DEFAULT_AWS_INSTANCE),
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
        # Also ensure Docker service start in fallback (defense in depth)
        try:
            main_tf = re.sub(
                r'(\"cd /opt/app && sudo -n make up\",)',
                '\"sudo -n systemctl enable --now docker\",\n      \\1',
                main_tf,
            )
        except Exception:
            pass

    # Normalize Terraform script to ensure archive layout matches extraction path
    def _normalize_tf(code: str) -> str:
        # Ensure we extract to /opt/ so the extracted folder /opt/app exists (archive root is 'app')
        code = re.sub(r"tar\s+-xzf\s+/home/ubuntu/app\.tar\.gz\s+-C\s+/opt/app\b", "tar -xzf /home/ubuntu/app.tar.gz -C /opt/", code)
        code = re.sub(r"tar\s+-xzf\s+/home/ubuntu/app\.tar\.gz\s+-C\s+/opt\b", "tar -xzf /home/ubuntu/app.tar.gz -C /opt/", code)
        # If any scripts cd into /opt/app/app, fix to /opt/app
        code = re.sub(r"cd\s+/opt/app/app\b", "cd /opt/app", code)
        return code

    main_tf = _normalize_tf(main_tf)

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

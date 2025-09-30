import os
import uuid
import logging

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, HttpUrl, Field

from .queue import JobManager, JobStatus
from .worker import process_deploy_request

# Configure root logger for stdout
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("api")

app = FastAPI(title="Repo Autodeployer", version="0.1.0")

# Initialize job manager with configurable concurrency
MAX_CONCURRENT_JOBS = int(os.environ.get("MAX_CONCURRENT_JOBS", "2"))
job_manager = JobManager(max_workers=MAX_CONCURRENT_JOBS)


class DeployRequest(BaseModel):
    description: str = Field(..., description="Natural language deployment requirements")
    repo_url: HttpUrl = Field(..., description="GitHub repository URL")


@app.post("/request")
async def request_deploy(payload: DeployRequest):
    job_id = str(uuid.uuid4())

    # Prepare a unique working directory for this job
    workdir = os.path.join("/data", "autodeploy", job_id)
    os.makedirs(workdir, exist_ok=True)

    logger.info("Enqueueing job %s for repo %s", job_id, payload.repo_url)
    job_manager.create_job(job_id, workdir)

    def task():
        process_deploy_request(
            job_manager=job_manager,
            job_id=job_id,
            description=payload.description,
            repo_url=str(payload.repo_url),
            workdir=workdir,
        )

    job_manager.submit(job_id, task)

    return {"job_id": job_id, "status": JobStatus.queued}


@app.get("/list")
async def list_jobs():
    return job_manager.list_jobs()


@app.get("/job/{job_id}")
async def get_job(job_id: str):
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job

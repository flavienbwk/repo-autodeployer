import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, Any, Optional


class JobStatus:
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"


class InMemoryJobLogger(logging.Handler):
    def __init__(self, job_logs: list):
        super().__init__()
        self.job_logs = job_logs

    def emit(self, record: logging.LogRecord) -> None:
        msg = self.format(record)
        self.job_logs.append(msg)


class JobManager:
    def __init__(self, max_workers: int = 2):
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._logger = logging.getLogger("JobManager")

    def create_job(self, job_id: str, workdir: str):
        with self._lock:
            self._jobs[job_id] = {
                "id": job_id,
                "status": JobStatus.queued,
                "workdir": workdir,
                "logs": [],
                "result": None,
                "error": None,
            }

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._jobs.get(job_id)

    def list_jobs(self):
        # Return job metadata without logs to keep the listing lightweight
        with self._lock:
            jobs = []
            for job in self._jobs.values():
                j = {k: v for k, v in job.items() if k != "logs"}
                j["log_count"] = len(job.get("logs", []))
                jobs.append(j)
            return jobs

    def _set_status(self, job_id: str, status: str):
        with self._lock:
            if job_id in self._jobs:
                self._jobs[job_id]["status"] = status

    def _append_log(self, job_id: str, message: str):
        with self._lock:
            if job_id in self._jobs:
                self._jobs[job_id]["logs"].append(message)

    def get_job_logger(self, job_id: str) -> logging.Logger:
        logger = logging.getLogger(f"job.{job_id}")
        logger.setLevel(logging.INFO)
        # Avoid duplicate handlers if called multiple times
        if not any(isinstance(h, InMemoryJobLogger) for h in logger.handlers):
            job = self.get_job(job_id)
            if job is not None:
                handler = InMemoryJobLogger(job["logs"]) 
                formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(name)s - %(message)s')
                handler.setFormatter(formatter)
                logger.addHandler(handler)
        return logger

    def submit(self, job_id: str, fn: Callable[[], None]):
        self._set_status(job_id, JobStatus.running)
        job_logger = self.get_job_logger(job_id)
        job_logger.info("Job started")

        def _wrapper():
            try:
                fn()
                self._set_status(job_id, JobStatus.completed)
                job_logger.info("Job completed successfully")
            except Exception as e:
                self._set_status(job_id, JobStatus.failed)
                job_logger.exception("Job failed: %s", e)

        self._executor.submit(_wrapper)

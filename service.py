"""Scheduled collection and REST access to the file-based school brief."""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from secrets import compare_digest
from threading import Lock
from typing import Any
from zoneinfo import ZoneInfo

import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.base import BaseTrigger
from croniter import croniter
from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

import daily_brief
import generate_brief
import portal_archive

LOGGER = logging.getLogger(__name__)


class MessageCronTrigger(BaseTrigger):
    def __init__(self, expression: str, timezone: ZoneInfo):
        if len(expression.split()) != 5 or not croniter.is_valid(expression):
            raise ValueError("MESSAGE_POLL_CRON must be a valid five-field cron expression")
        self.expression = expression
        self.timezone = timezone

    def get_next_fire_time(self, previous_fire_time: datetime | None, now: datetime) -> datetime:
        base = previous_fire_time if previous_fire_time is not None else now - timedelta(microseconds=1)
        return croniter(self.expression, base.astimezone(self.timezone)).get_next(datetime)


def prepare_storage(data: dict[str, Any]) -> dict[str, Any]:
    warnings = [f"{name} fetch failed; retained messages were reused."
                for name, source in data.items() if source.get("error")]
    documents = 0
    has_pdfs = any(
        attachment.get("saved_as")
        for source in data.values() for message in source.get("messages", [])
        for attachment in message.get("attachments", [])
    )
    if has_pdfs:
        client, deployment = generate_brief.create_client()
        try:
            payload, pdf_warnings, _ = generate_brief.prepare_input(
                data, client=client, deployment=deployment,
            )
            documents = len(payload["documents"])
            warnings.extend(warning for warning in pdf_warnings
                            if not any(warning.startswith(f"{name}:") for name in data))
        finally:
            client.close()
    return {
        "messages": {name: len(source.get("messages", [])) for name, source in data.items()},
        "pdf_documents": documents,
        "warnings": list(dict.fromkeys(warnings)),
    }


def create_app() -> FastAPI:
    cron = os.getenv("MESSAGE_POLL_CRON", "*/30 * * * *")
    timezone = ZoneInfo(os.getenv("MESSAGE_POLL_TIMEZONE", "Asia/Kolkata"))
    trigger = MessageCronTrigger(cron, timezone)
    token = os.getenv("SERVICE_API_TOKEN", "").strip()
    operation_lock = Lock()
    scheduler = BackgroundScheduler(timezone=timezone)

    def poll_messages() -> None:
        if not operation_lock.acquire(blocking=False):
            LOGGER.info("Skipping collection while another storage operation is running")
            return
        try:
            data = daily_brief.run()
            summary = prepare_storage(data)
            application.state.last_poll = {
                "at": datetime.now(timezone).isoformat(), **summary,
            }
            LOGGER.info("Scheduled collection completed: %s", summary["messages"])
        except Exception as error:
            application.state.last_poll = {
                "at": datetime.now(timezone).isoformat(), "error": type(error).__name__,
            }
            LOGGER.error("Scheduled collection failed (%s)", type(error).__name__)
        finally:
            operation_lock.release()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        scheduler.add_job(poll_messages, trigger, id="collect_messages",
                          max_instances=1, coalesce=True, misfire_grace_time=300)
        scheduler.start()
        try:
            yield
        finally:
            await asyncio.to_thread(scheduler.shutdown, wait=True)

    application = FastAPI(title="Daily School Brief", lifespan=lifespan)
    application.state.operation_lock = operation_lock
    application.state.scheduler = scheduler
    application.state.last_poll = None

    def authorize(credentials: HTTPAuthorizationCredentials | None = Depends(HTTPBearer(auto_error=False))) -> None:
        if token and (credentials is None or not compare_digest(credentials.credentials, token)):
            raise HTTPException(status_code=401, detail="A valid bearer token is required",
                                headers={"WWW-Authenticate": "Bearer"})

    @application.get("/health", dependencies=[Depends(authorize)])
    def health() -> dict[str, Any]:
        job = scheduler.get_job("collect_messages")
        return {
            "status": "ok", "initialized": portal_archive.ARCHIVE_PATH.exists(),
            "cron": cron, "timezone": str(timezone),
            "retention_days": portal_archive.RETENTION_DAYS,
            "next_poll": job.next_run_time.isoformat() if job and job.next_run_time else None,
            "busy": operation_lock.locked(), "last_poll": application.state.last_poll,
        }

    @application.post("/initialize", dependencies=[Depends(authorize)])
    def initialize_storage() -> dict[str, Any]:
        if not operation_lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="Another storage operation is running; retry later")
        try:
            data = daily_brief.run(initialize=True)
            if data["portal"].get("error"):
                raise HTTPException(status_code=502, detail="Portal initialization failed; existing storage was preserved")
            return prepare_storage(data)
        except HTTPException:
            raise
        except Exception as error:
            LOGGER.error("Storage initialization failed (%s)", type(error).__name__)
            raise HTTPException(status_code=502, detail=f"Storage initialization failed ({type(error).__name__})") from error
        finally:
            operation_lock.release()

    @application.post("/brief", response_model=generate_brief.BriefContent,
                      dependencies=[Depends(authorize)])
    def prepare_brief() -> generate_brief.BriefContent:
        if not operation_lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="Another storage operation is running; retry later")
        try:
            if not portal_archive.ARCHIVE_PATH.exists() and not portal_archive.WHATSAPP_ARCHIVE_PATH.exists():
                raise HTTPException(status_code=409, detail="Initialize storage first with POST /initialize")
            now = datetime.now(portal_archive.IST)
            archives = portal_archive.cleanup_stored_data(now)
            saved_input = json.loads(daily_brief.OUTPUT_PATH.read_text(encoding="utf-8")) if daily_brief.OUTPUT_PATH.exists() else {}
            data = {name: {"messages": messages, "error": saved_input.get(name, {}).get("error")}
                    for name, messages in archives.items()}
            content = generate_brief.generate(data, now=now)
            generate_brief.write_content(content)
            return content
        except HTTPException:
            raise
        except Exception as error:
            LOGGER.error("Stored-data brief generation failed (%s)", type(error).__name__)
            raise HTTPException(status_code=502, detail=f"Brief generation failed ({type(error).__name__})") from error
        finally:
            operation_lock.release()

    return application


app = create_app()


def run() -> None:
    host = os.getenv("SERVICE_HOST", "127.0.0.1")
    if host not in {"127.0.0.1", "localhost", "::1"} and not os.getenv("SERVICE_API_TOKEN", "").strip():
        raise ValueError("Set SERVICE_API_TOKEN before binding outside localhost")
    uvicorn.run(app, host=host, port=int(os.getenv("SERVICE_PORT", "8000")))


if __name__ == "__main__":
    run()
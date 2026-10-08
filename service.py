"""Scheduled collection and REST access to the file-based school brief."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from secrets import compare_digest
from threading import Lock
from time import monotonic
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.base import BaseTrigger
from croniter import croniter
from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

import daily_brief
import generate_brief
import portal_archive
import send_email

LOGGER = logging.getLogger(__name__)


def format_poll_time(timestamp: datetime) -> str:
    local_time = timestamp.astimezone(portal_archive.IST)
    return f"{local_time.day} {local_time.strftime('%b %Y %I:%M:%S %p')}"


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

    def collect_and_generate() -> dict[str, Any]:
        if not operation_lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="Another storage operation is running; retry later")
        started = monotonic()
        try:
            data = daily_brief.run()
            summary = prepare_storage(data)
            LOGGER.info("Collection completed; generating saved brief")
            content = generate_brief.run(data=data, now=datetime.now(portal_archive.IST))
            warnings = list(dict.fromkeys(summary.pop("warnings", []) + content.warnings))
            application.state.last_poll = {
                "at": format_poll_time(datetime.now(timezone)), **summary,
                "brief_generated": True, "duration_seconds": round(monotonic() - started, 2),
            }
            LOGGER.info("Collection and brief generation completed in %.2fs", monotonic() - started)
            return {**application.state.last_poll, "warnings": warnings}
        except Exception as error:
            application.state.last_poll = {
                "at": format_poll_time(datetime.now(timezone)), "error": type(error).__name__,
                "brief_generated": False,
            }
            LOGGER.exception("Collection and brief generation failed (%s)", type(error).__name__)
            raise HTTPException(status_code=502, detail=f"Refresh failed ({type(error).__name__})") from error
        finally:
            operation_lock.release()

    def poll_messages() -> None:
        try:
            result = collect_and_generate()
            for warning in result["warnings"]:
                LOGGER.warning("Scheduled brief warning: %s", warning)
        except HTTPException as error:
            if error.status_code == 409:
                LOGGER.info("Skipping collection while another storage operation is running")

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

    @application.get("/favicon.ico", include_in_schema=False, status_code=204)
    def favicon() -> Response:
        return Response(status_code=204)

    def authorize(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(HTTPBearer(auto_error=False))],
    ) -> None:
        if token and (credentials is None or not compare_digest(credentials.credentials, token)):
            raise HTTPException(status_code=401, detail="A valid bearer token is required",
                                headers={"WWW-Authenticate": "Bearer"})

    @application.get("/pdfs/{filename}", response_class=FileResponse)
    def read_pdf(filename: str) -> FileResponse:
        directory = generate_brief.PDF_DIR.resolve()
        path = (directory / filename).resolve()
        if path.parent != directory or path.suffix.lower() != ".pdf" or not path.is_file():
            raise HTTPException(status_code=404, detail="PDF not found")
        return FileResponse(path, media_type="application/pdf", filename=path.name,
                            content_disposition_type="inline")

    @application.get("/health", dependencies=[Depends(authorize)])
    def health() -> dict[str, Any]:
        job = scheduler.get_job("collect_messages")
        return {
            "status": "ok", "initialized": portal_archive.ARCHIVE_PATH.exists(),
            "cron": cron, "timezone": str(timezone),
            "retention_days": portal_archive.RETENTION_DAYS,
            "next_poll": format_poll_time(job.next_run_time) if job and job.next_run_time else None,
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

    def collect_source(source: Literal["portal", "whatsapp"]) -> dict[str, Any]:
        if not operation_lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="Another storage operation is running; retry later")
        try:
            data = daily_brief.run(source=source)
            if data[source].get("error"):
                raise HTTPException(status_code=502, detail=f"{source} collection failed; cursor was not advanced")
            summary = prepare_storage({source: data[source]})
            summary.pop("warnings", None)
            return summary
        except HTTPException:
            raise
        except Exception as error:
            LOGGER.error("%s collection failed (%s)", source, type(error).__name__)
            raise HTTPException(status_code=502, detail=f"Collection failed ({type(error).__name__})") from error
        finally:
            operation_lock.release()

    @application.post("/scrape-udt", dependencies=[Depends(authorize)])
    def scrape_udt() -> dict[str, Any]:
        return collect_source("portal")

    @application.post("/fetch-whatsapp", dependencies=[Depends(authorize)])
    def fetch_whatsapp() -> dict[str, Any]:
        return collect_source("whatsapp")

    @application.post("/refresh", dependencies=[Depends(authorize)])
    def refresh_brief() -> dict[str, Any]:
        return collect_and_generate()

    @application.get("/brief", response_model=generate_brief.BriefContent,
                     response_model_exclude={"warnings"},
                     dependencies=[Depends(authorize)])
    @application.post("/brief", response_model=generate_brief.BriefContent,
                      response_model_exclude={"warnings"},
                      dependencies=[Depends(authorize)])
    def read_brief() -> generate_brief.BriefContent:
        try:
            return generate_brief.validate_brief_content(
                generate_brief.CONTENT_PATH.read_text(encoding="utf-8"),
            )
        except FileNotFoundError as error:
            raise HTTPException(status_code=409, detail="No saved brief; run POST /refresh or wait for cron") from error
        except Exception as error:
            LOGGER.error("Reading saved brief failed (%s)", type(error).__name__)
            raise HTTPException(status_code=502, detail=f"Saved brief unavailable ({type(error).__name__})") from error

    @application.post("/email-brief", dependencies=[Depends(authorize)])
    def email_brief() -> dict[str, str]:
        if not operation_lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="Another storage operation is running; retry later")
        try:
            if not send_email.CONTENT_PATH.exists():
                raise HTTPException(status_code=409, detail="Generate a brief first with POST /refresh")
            generate_brief.validate_brief_content(send_email.CONTENT_PATH.read_text(encoding="utf-8"))
            send_email.run()
            return {"status": "sent"}
        except HTTPException:
            raise
        except Exception as error:
            LOGGER.error("Email delivery failed (%s)", type(error).__name__)
            raise HTTPException(status_code=502, detail=f"Email delivery failed ({type(error).__name__})") from error
        finally:
            operation_lock.release()

    return application


app = create_app()


def run() -> None:
    logging.basicConfig(level=os.getenv("SERVICE_LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    host = os.getenv("SERVICE_HOST", "127.0.0.1")
    if host not in {"127.0.0.1", "localhost", "::1"} and not os.getenv("SERVICE_API_TOKEN", "").strip():
        raise ValueError("Set SERVICE_API_TOKEN before binding outside localhost")
    reload = os.getenv("SERVICE_RELOAD", "").strip().lower() == "true"
    uvicorn.run("service:app" if reload else app, host=host,
                port=int(os.getenv("SERVICE_PORT", "8000")), reload=reload)


if __name__ == "__main__":
    run()
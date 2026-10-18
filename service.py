"""Scheduled collection and REST access to the file-based school brief."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from secrets import compare_digest
from threading import Lock
from time import monotonic
from typing import Annotated, Any, Literal, TypedDict
from urllib.parse import quote
from zoneinfo import ZoneInfo

import uvicorn
import httpx
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.base import BaseTrigger
from croniter import croniter
from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

import daily_brief
import fetch_evolution
import generate_brief
import portal_archive
import scrape_udt as portal

LOGGER = logging.getLogger(__name__)


class ConnectionStatus(TypedDict):
    connected: bool | None
    state: str
    checked_at: str | None
    error: str | None


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
    warnings.extend(generate_brief.attachment_warnings(data))
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
        "messages": {name: len(source.get("messages", [])) for name, source in data.items()
                     if name != "attendance"},
        "attendance_records": len(data.get("attendance", {}).get("records", [])),
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

    def record_connection(name: str, connected: bool | None, state: str, error: str | None = None) -> None:
        status: ConnectionStatus = {
            "connected": connected, "state": state,
            "checked_at": datetime.now(timezone).isoformat(), "error": error,
        }
        application.state.connections = {**application.state.connections, name: status}

    def record_collection_connections(data: dict[str, Any], sources: tuple[str, ...]) -> None:
        for source in sources:
            if source not in data:
                continue
            name = "udt" if source == "portal" else "whatsapp"
            if data[source].get("error"):
                record_connection(name, None, "collection_failed", "CollectionFailed")
            else:
                record_connection(name, True, "authenticated" if source == "portal" else "open")

    def probe_connections() -> None:
        try:
            portal.check_connection()
        except (requests.RequestException, ValueError, RuntimeError) as error:
            LOGGER.warning("UDT connection check failed (%s)", type(error).__name__)
            record_connection("udt", False, "error", type(error).__name__)
        else:
            record_connection("udt", True, "authenticated")
        try:
            settings = fetch_evolution.load_settings()
            with fetch_evolution.create_client(settings) as client:
                state = fetch_evolution.connection_state(client, quote(settings["EVOLUTION_INSTANCE"], safe=""))
        except (httpx.HTTPError, ValueError, TypeError, RuntimeError) as error:
            LOGGER.warning("WhatsApp connection check failed (%s)", type(error).__name__)
            record_connection("whatsapp", False, "error", type(error).__name__)
        else:
            record_connection("whatsapp", state == "open", state)

    def collect_and_generate() -> dict[str, Any]:
        if not operation_lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="Another storage operation is running; retry later")
        started = monotonic()
        try:
            data = daily_brief.run()
            record_collection_connections(data, ("portal", "whatsapp"))
            summary = prepare_storage(data)
            LOGGER.info("Collection completed; generating saved brief")
            content, reused = generate_brief.run_with_status(data=data, now=datetime.now(portal_archive.IST))
            warnings = list(dict.fromkeys(summary.pop("warnings", []) + content.warnings))
            application.state.last_poll = {
                "at": format_poll_time(datetime.now(timezone)), **summary,
                "brief_generated": not reused, "brief_reused": reused,
                "duration_seconds": round(monotonic() - started, 2),
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
    application.state.connections = {
        name: ConnectionStatus(connected=None, state="unknown", checked_at=None, error=None)
        for name in ("udt", "whatsapp")
    }

    @application.get("/favicon.ico", include_in_schema=False, status_code=204)
    def favicon() -> Response:
        return Response(status_code=204)

    def authorize(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(HTTPBearer(auto_error=False))],
    ) -> None:
        if not token:
            raise HTTPException(status_code=503, detail="Configure SERVICE_API_TOKEN before accessing the API",
                                headers={"Cache-Control": "no-store"})
        if credentials is None or not compare_digest(credentials.credentials, token):
            raise HTTPException(status_code=401, detail="A valid bearer token is required",
                                headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"})

    @application.get("/pdfs/{filename}", response_class=FileResponse, dependencies=[Depends(authorize)])
    def read_pdf(filename: str) -> FileResponse:
        directory = generate_brief.PDF_DIR.resolve()
        path = (directory / filename).resolve()
        if path.parent != directory or path.suffix.lower() != ".pdf" or not path.is_file():
            raise HTTPException(status_code=404, detail="PDF not found")
        return FileResponse(path, media_type="application/pdf", filename=path.name,
                            content_disposition_type="inline")

    @application.get("/health", dependencies=[Depends(authorize)])
    def health(check_connections: bool = False) -> dict[str, Any]:
        if check_connections:
            if not operation_lock.acquire(blocking=False):
                raise HTTPException(status_code=409, detail="Another operation is running; retry the connection check later")
            try:
                probe_connections()
            finally:
                operation_lock.release()
        job = scheduler.get_job("collect_messages")
        now = datetime.now(timezone)
        return {
            "status": "ok", "initialized": portal_archive.ARCHIVE_PATH.exists(),
            "cron": cron, "timezone": str(timezone),
            "retention_days": portal_archive.RETENTION_DAYS,
            "next_poll": format_poll_time(job.next_run_time) if job and job.next_run_time else None,
            "busy": operation_lock.locked(), "last_poll": application.state.last_poll,
            "connections": {
                name: {**status, "stale": status["checked_at"] is None or
                       now - datetime.fromisoformat(status["checked_at"]) > timedelta(minutes=5)}
                for name, status in application.state.connections.items()
            },
        }

    @application.post("/whatsapp-login", response_class=Response, dependencies=[Depends(authorize)],
                      responses={200: {"content": {"image/png": {}}, "description": "WhatsApp pairing QR PNG"},
                                 202: {"description": "QR not ready; retry shortly"},
                                 409: {"description": "Already connected, or another operation is running"}})
    def whatsapp_qr() -> Response:
        headers = {"Cache-Control": "no-store", "Pragma": "no-cache", "X-Content-Type-Options": "nosniff"}
        if not operation_lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="Another operation is running; retry later", headers=headers)
        try:
            settings = fetch_evolution.load_settings()
            with fetch_evolution.create_client(settings) as client:
                image = fetch_evolution.pairing_qr(client, quote(settings["EVOLUTION_INSTANCE"], safe=""))
            if image is None:
                record_connection("whatsapp", True, "open")
                raise HTTPException(status_code=409, detail="WhatsApp is already connected; no QR scan is needed",
                                    headers=headers)
            record_connection("whatsapp", False, "qr_required")
            return Response(image, media_type="image/png", headers=headers)
        except fetch_evolution.PairingPending:
            record_connection("whatsapp", False, "connecting")
            return JSONResponse(status_code=202, content={"status": "pending", "detail": "QR not ready; retry shortly"},
                                headers={**headers, "Retry-After": "2"})
        except (httpx.HTTPError, ValueError, TypeError, RuntimeError) as error:
            LOGGER.warning("WhatsApp pairing failed (%s)", type(error).__name__)
            record_connection("whatsapp", None, "error", type(error).__name__)
            raise HTTPException(status_code=502, detail=f"WhatsApp pairing failed ({type(error).__name__})",
                                headers=headers) from error
        finally:
            operation_lock.release()

    @application.post("/initialize", dependencies=[Depends(authorize)])
    def initialize_storage() -> dict[str, Any]:
        if not operation_lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="Another storage operation is running; retry later")
        try:
            data = daily_brief.run(initialize=True)
            record_collection_connections(data, ("portal", "whatsapp"))
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
            record_collection_connections(data, (source,))
            if data[source].get("error"):
                raise HTTPException(status_code=502, detail=f"{source} collection failed; cursor was not advanced")
            collected: dict[str, Any] = {source: data[source]}
            if source == "portal" and "attendance" in data:
                collected["attendance"] = data["attendance"]
            summary = prepare_storage(collected)
            summary.pop("warnings", None)
            download_warnings = generate_brief.attachment_warnings(collected)
            if download_warnings:
                summary["warnings"] = download_warnings
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
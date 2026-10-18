"""Read school-group text and PDFs from a self-hosted Evolution API v2 instance."""

import base64
import hashlib
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

PAGE_SIZE = 100
MAX_PDF_BYTES = 25 * 1024 * 1024
LOGGER = logging.getLogger(__name__)


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"Evolution returned invalid {label}")
    return value


def _content(message: dict[str, Any]) -> dict[str, Any]:
    for _ in range(8):
        for wrapper in (
            "ephemeralMessage", "viewOnceMessage", "viewOnceMessageV2",
            "viewOnceMessageV2Extension", "documentWithCaptionMessage",
        ):
            if wrapper in message:
                message = _object(_object(message[wrapper], wrapper).get("message"), "wrapped message")
                break
        else:
            return message
    raise ValueError("Evolution message nesting is too deep")


def _pdf_name(document: dict[str, Any]) -> str:
    filename = document.get("fileName") or "document.pdf"
    if not isinstance(filename, str):
        raise TypeError("Evolution returned an invalid PDF filename")
    return filename.replace("\\", "/").rsplit("/", 1)[-1] or "document.pdf"


def _save_pdf(
    client: httpx.Client, instance: str, key: dict[str, Any],
    document: dict[str, Any], identity: str, save_dir: Path,
) -> dict[str, str]:
    filename = _pdf_name(document)
    destination = save_dir / f"whatsapp_evolution_{identity}.pdf"
    if not destination.exists():
        response = client.post(
            f"chat/getBase64FromMediaMessage/{instance}",
            json={"message": {"key": key}, "convertToMp4": False},
        )
        response.raise_for_status()
        payload = _object(response.json(), "media response").get("base64")
        if not isinstance(payload, str) or not payload:
            raise ValueError("Evolution returned no PDF data")
        if payload.startswith("data:"):
            header, separator, payload = payload.partition(",")
            if not separator or not header.endswith(";base64"):
                raise ValueError("Evolution returned an invalid PDF data URI")
        if len(payload) > ((MAX_PDF_BYTES + 2) // 3) * 4:
            raise ValueError("Evolution PDF exceeds the 25 MiB limit")
        content = base64.b64decode(payload, validate=True)
        if len(content) > MAX_PDF_BYTES or not content.startswith(b"%PDF-"):
            raise ValueError("Evolution returned invalid or oversized PDF data")
        save_dir.mkdir(parents=True, exist_ok=True)
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(dir=save_dir, suffix=".tmp", delete=False) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(content)
            os.replace(temporary_path, destination)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
    return {"name": filename, "saved_as": str(destination)}


def load_settings() -> dict[str, str]:
    settings = {
        name: os.getenv(name, "").strip()
        for name in ("EVOLUTION_API_URL", "EVOLUTION_API_KEY", "EVOLUTION_INSTANCE")
    }
    missing = [name for name, value in settings.items() if not value]
    if missing:
        raise ValueError(f"Missing Evolution configuration: {', '.join(missing)}")
    url = urlsplit(settings["EVOLUTION_API_URL"])
    if (
        url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password
        or url.query or url.fragment
    ):
        raise ValueError("EVOLUTION_API_URL must be an HTTP(S) URL without credentials, query or fragment")
    return settings


def create_client(settings: dict[str, str]) -> httpx.Client:
    return httpx.Client(
        base_url=settings["EVOLUTION_API_URL"].rstrip("/") + "/",
        headers={"apikey": settings["EVOLUTION_API_KEY"]}, timeout=60, follow_redirects=False,
    )


def fetch_recent_evolution_messages(cutoff: datetime, save_dir: Path) -> list[dict[str, Any]]:
    settings = load_settings()
    group = os.getenv("EVOLUTION_GROUP_JID", "").strip()
    if not group.endswith("@g.us") or len(group) <= len("@g.us"):
        raise ValueError("EVOLUTION_GROUP_JID must be an exact WhatsApp group ID ending in @g.us")
    instance = quote(settings["EVOLUTION_INSTANCE"], safe="")
    cutoff = cutoff.astimezone(timezone.utc)
    # Evolution truncates query timestamps to seconds; round up to avoid replaying the cursor's second.
    if cutoff.microsecond:
        cutoff = cutoff.replace(microsecond=0) + timedelta(seconds=1)
    until = datetime.now(timezone.utc)
    messages: dict[str, dict[str, Any]] = {}
    with create_client(settings) as client:
        response = client.get(f"instance/connectionState/{instance}")
        response.raise_for_status()
        state = _object(_object(response.json(), "connection response").get("instance"), "instance").get("state")
        if state != "open":
            raise RuntimeError("Evolution WhatsApp instance is disconnected; reconnect it before collecting")
        for page in range(1, 10001):
            response = client.post(f"chat/findMessages/{instance}", json={
                "where": {
                    "key": {"remoteJid": group, "remoteJidAlt": group},
                    "messageTimestamp": {"gte": cutoff.isoformat(), "lte": until.isoformat()},
                },
                "page": page, "offset": PAGE_SIZE,
            })
            response.raise_for_status()
            batch = _object(_object(response.json(), "message response").get("messages"), "messages")
            records, pages = batch.get("records"), batch.get("pages")
            if (
                not isinstance(records, list) or type(pages) is not int or pages < 0
                or batch.get("currentPage") != page
                or (not records and pages >= page)
                or (records and pages < page)
            ):
                raise ValueError("Evolution returned invalid message pagination")
            for raw_record in records:
                record = _object(raw_record, "message record")
                key = _object(record.get("key"), "message key")
                if group not in (key.get("remoteJid"), key.get("remoteJidAlt")):
                    continue
                raw_timestamp = record.get("messageTimestamp")
                if isinstance(raw_timestamp, bool) or not isinstance(raw_timestamp, (int, str)):
                    raise TypeError("Evolution returned an invalid message timestamp")
                timestamp = datetime.fromtimestamp(int(raw_timestamp), timezone.utc)
                if timestamp < cutoff or timestamp > until:
                    continue
                message_id = key.get("id")
                if not isinstance(message_id, str) or not message_id:
                    raise ValueError("Evolution returned a message without an ID")
                identity = hashlib.sha256(
                    f"{settings['EVOLUTION_INSTANCE']}\0{group}\0{message_id}".encode(),
                ).hexdigest()
                content = _content(_object(record.get("message"), "message content"))
                text = content.get("conversation", "")
                for kind, field in (
                    ("extendedTextMessage", "text"), ("documentMessage", "caption"),
                    ("imageMessage", "caption"), ("videoMessage", "caption"),
                ):
                    if kind in content:
                        text = _object(content[kind], kind).get(field, "")
                        break
                if not isinstance(text, str):
                    raise TypeError("Evolution returned invalid message text")
                attachments = []
                if "documentMessage" in content:
                    document = _object(content["documentMessage"], "document")
                    filename = document.get("fileName", "")
                    if not isinstance(filename, str):
                        raise ValueError("Evolution returned an invalid document filename")
                    if document.get("mimetype") == "application/pdf" or filename.lower().endswith(".pdf"):
                        try:
                            attachments.append(_save_pdf(client, instance, key, document, identity, save_dir))
                        except (httpx.HTTPStatusError, httpx.RequestError) as error:
                            reason = (
                                f"HTTP {error.response.status_code}"
                                if isinstance(error, httpx.HTTPStatusError) else type(error).__name__
                            )
                            warning = (
                                f"Evolution PDF download failed ({reason}); "
                                "retry requires explicit initialization."
                            )
                            LOGGER.warning("PDF %s: %s", identity, warning)
                            attachments.append({"name": _pdf_name(document), "download_error": warning})
                # Non-PDF media and WhatsApp protocol events are not brief inputs.
                if not text.strip() and not attachments:
                    continue
                sender = record.get("pushName") or key.get("participant") or (
                    "You" if key.get("fromMe") else "Unknown sender"
                )
                if not isinstance(sender, str):
                    raise TypeError("Evolution returned an invalid sender")
                messages[identity] = {
                    "id": f"evolution:{identity}", "timestamp": timestamp.isoformat(),
                    "sender": sender, "text": text.strip(), "attachments": attachments,
                }
            if page >= pages:
                break
        else:
            raise ValueError("Evolution message pagination exceeded 10000 pages")
    return sorted(messages.values(), key=lambda message: message["timestamp"])

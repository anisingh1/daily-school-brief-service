"""Pair Evolution with WhatsApp and select a school group without printing API keys."""

import argparse
import base64
import os
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv, set_key

from fetch_evolution import create_client, load_settings

ROOT = Path(__file__).parent
ENV_PATH = ROOT / ".env"
QR_PATH = ROOT / "output" / "evolution_qr.png"


def run(command: str, group_jid: str | None = None) -> None:
    load_dotenv(ENV_PATH)
    settings = load_settings()
    instance = quote(settings["EVOLUTION_INSTANCE"], safe="")
    with create_client(settings) as client:
        if command == "create":
            response = client.post("instance/create", json={
                "instanceName": settings["EVOLUTION_INSTANCE"],
                "integration": "WHATSAPP-BAILEYS", "qrcode": False,
                "groupsIgnore": False, "syncFullHistory": True,
                "alwaysOnline": False, "readMessages": False, "readStatus": False,
            })
            response.raise_for_status()
            print("Created Evolution instance. Run the pair command next.")
        elif command == "pair":
            response = client.get(f"instance/connect/{instance}")
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError("Evolution returned an invalid pairing response")
            if result.get("instance", {}).get("state") == "open":
                QR_PATH.unlink(missing_ok=True)
                print("WhatsApp is already connected. Run the groups command next.")
                return
            encoded = result.get("base64")
            if not isinstance(encoded, str) or not encoded:
                QR_PATH.unlink(missing_ok=True)
                raise ValueError("No QR code available yet; retry the pair command shortly")
            image = base64.b64decode(encoded.removeprefix("data:image/png;base64,"), validate=True)
            if not image.startswith(b"\x89PNG\r\n\x1a\n"):
                raise ValueError("Evolution returned an invalid QR image")
            QR_PATH.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(QR_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(image)
            print(f"Open {QR_PATH} and scan it in WhatsApp > Linked devices > Link a device.")
            print("QR codes expire; rerun pair to refresh. Do not share this image.")
        elif command == "status":
            response = client.get(f"instance/connectionState/{instance}")
            response.raise_for_status()
            state = response.json()["instance"]["state"]
            print(f"WhatsApp connection: {state}")
            if state != "open":
                raise RuntimeError("WhatsApp is not connected; run the pair command")
            QR_PATH.unlink(missing_ok=True)
        elif command in {"groups", "select-group"}:
            response = client.get(f"group/fetchAllGroups/{instance}", params={"getParticipants": "false"})
            response.raise_for_status()
            groups = response.json()
            if not isinstance(groups, list) or any(
                not isinstance(group, dict) or not isinstance(group.get("id"), str)
                or not group["id"].endswith("@g.us") for group in groups
            ):
                raise ValueError("Evolution returned an invalid group list")
            if command == "groups":
                for group in groups:
                    print(f"{group['id']}\t{group.get('subject', '')}")
                if not groups:
                    print("No groups available yet; wait for sync and retry.")
            else:
                if group_jid is None or group_jid not in {group["id"] for group in groups}:
                    raise ValueError("Select an exact group ID returned by the groups command")
                if not ENV_PATH.is_file():
                    raise FileNotFoundError("Create the application's .env before selecting a group")
                set_key(str(ENV_PATH), "EVOLUTION_GROUP_JID", group_jid)
                set_key(str(ENV_PATH), "WHATSAPP_SOURCE", "evolution")
                QR_PATH.unlink(missing_ok=True)
                print("Enabled direct Evolution ingestion for the selected group. Restart the brief service.")
        else:
            raise ValueError("Unknown Evolution administration command")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["create", "pair", "status", "groups", "select-group"])
    parser.add_argument("group_jid", nargs="?", help="Exact group ID for select-group")
    args = parser.parse_args()
    if args.command == "select-group" and not args.group_jid:
        parser.error("select-group requires a group ID")
    run(args.command, args.group_jid)


if __name__ == "__main__":
    main()

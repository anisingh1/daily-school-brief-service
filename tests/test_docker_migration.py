import json
from pathlib import Path

import pytest

from docker.migrate_local_data import stage_data


@pytest.fixture
def native_data(tmp_path):
    root = tmp_path / "native"
    (root / "data" / "pdfs").mkdir(parents=True)
    (root / "output").mkdir()
    pdf = root / "data" / "pdfs" / "worksheet.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    message = {"id": "stable", "attachments": [{"saved_as": str(pdf), "name": pdf.name}]}
    (root / "data" / "whatsapp_messages.json").write_text(json.dumps([message]))
    (root / "data" / "last_run.json").write_text('{"whatsapp_last_run":"2026-10-18T12:00:00"}')
    (root / "output" / "daily_brief_input.json").write_text(json.dumps({"whatsapp": {"messages": [message]}}))
    (root / "output" / "daily_brief_content.json").write_text('{"homework":["Read page 12"]}')
    (root / "output" / "daily_brief_content.cache.json").write_text('{"input_hash":"native-only"}')
    return root


def test_migration_copies_files_rewrites_only_paths_and_preserves_native_data(native_data, tmp_path):
    before = {path.relative_to(native_data): path.read_bytes() for path in native_data.rglob("*") if path.is_file()}
    destination = tmp_path / "staged"
    stage_data(native_data, destination)
    messages = json.loads((destination / "data" / "whatsapp_messages.json").read_text())
    assert messages[0]["attachments"][0]["saved_as"] == "/app/data/pdfs/worksheet.pdf"
    saved_input = json.loads((destination / "output" / "daily_brief_input.json").read_text())
    assert saved_input["whatsapp"]["messages"] == messages
    assert (destination / "data" / "pdfs" / "worksheet.pdf").read_bytes() == b"%PDF-1.4\n"
    assert (destination / "data" / "last_run.json").read_bytes() == before[Path("data/last_run.json")]
    assert (destination / "output" / "daily_brief_content.json").read_bytes() == before[Path("output/daily_brief_content.json")]
    assert not (destination / "output" / "daily_brief_content.cache.json").exists()
    assert {path.relative_to(native_data): path.read_bytes() for path in native_data.rglob("*") if path.is_file()} == before


def test_migration_refuses_to_overwrite_destination(native_data, tmp_path):
    destination = tmp_path / "existing"
    destination.mkdir()
    with pytest.raises(FileExistsError):
        stage_data(native_data, destination)


def test_migration_rejects_external_attachment_paths_before_copying(native_data, tmp_path):
    (native_data / "data" / "whatsapp_messages.json").write_text('[{"saved_as":"/private/file.pdf"}]')
    destination = tmp_path / "staged"
    with pytest.raises(ValueError):
        stage_data(native_data, destination)
    assert not destination.exists()


def test_migration_rejects_symlinks(native_data, tmp_path):
    (native_data / "data" / "linked").symlink_to(tmp_path)
    with pytest.raises(ValueError, match="symlink"):
        stage_data(native_data, tmp_path / "staged")


@pytest.mark.parametrize("directory", ["data", "output"])
def test_migration_rejects_recursive_destination(native_data, directory):
    destination = native_data / directory / "migration"
    with pytest.raises(ValueError, match="inside native"):
        stage_data(native_data, destination)
    assert not destination.exists()

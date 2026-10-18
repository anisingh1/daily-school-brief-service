"""Stage native application files for import into Docker, without changing originals."""

import argparse
import json
import shutil
from pathlib import Path
from typing import Any


def rewrite_paths(value: Any, source: Path) -> Any:
    if isinstance(value, list):
        return [rewrite_paths(item, source) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: rewrite_paths(item, source) for key, item in value.items()}
    saved_as = result.get("saved_as")
    if isinstance(saved_as, str) and Path(saved_as).is_absolute():
        relative = Path(saved_as).resolve().relative_to((source / "data" / "pdfs").resolve())
        result["saved_as"] = str(Path("/app/data/pdfs") / relative)
    return result


def stage_data(source: Path, destination: Path) -> None:
    source = source.resolve()
    destination = destination.resolve()
    if destination.exists():
        raise FileExistsError(f"Refusing to overwrite existing migration destination: {destination}")
    if destination == source or source.is_relative_to(destination):
        raise ValueError("Migration destination must not contain the native repository")
    if any(destination.is_relative_to(source / directory) for directory in ("data", "output")):
        raise ValueError("Migration destination must not be inside native data or output")
    for directory in ("data", "output"):
        if not (source / directory).is_dir():
            raise FileNotFoundError(f"Missing native {directory} directory")
        for path in (source / directory).rglob("*"):
            if path.is_symlink():
                raise ValueError(f"Refusing to migrate a symlink: {path}")
            if path.suffix == ".json" and path.is_file():
                rewrite_paths(json.loads(path.read_text(encoding="utf-8")), source)
    destination.mkdir(parents=True)
    for directory in ("data", "output"):
        shutil.copytree(source / directory, destination / directory)
        for path in (destination / directory).rglob("*.json"):
            original = json.loads(path.read_text(encoding="utf-8"))
            rewritten = rewrite_paths(original, source)
            if rewritten != original:
                path.write_text(json.dumps(rewritten, ensure_ascii=False, indent=2), encoding="utf-8")
    (destination / "output" / "daily_brief_content.cache.json").unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    stage_data(args.source, args.destination)
    print(f"Staged native files in {args.destination}; original files were not modified.")


if __name__ == "__main__":
    main()

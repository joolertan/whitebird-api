"""Command line interface: ``python -m whitebird_codegen``."""

from __future__ import annotations

import argparse
import fnmatch
import json
import sys
from dataclasses import asdict
from pathlib import Path

import yaml

from . import emitters, importer
from .build import SpecError, load

REPO = Path(__file__).resolve().parents[4]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="whitebird-codegen")
    parser.add_argument("--spec", type=Path, default=REPO / "spec")
    parser.add_argument("--root", type=Path, default=REPO)
    commands = parser.add_subparsers(dest="command", required=True)
    generate = commands.add_parser("generate", help="write generated code")
    generate.add_argument("--lang", action="append", choices=emitters.LANGUAGES)
    generate.add_argument("--check", action="store_true", help="fail if generated code is stale")
    commands.add_parser("dump-ir", help="print the intermediate representation as JSON")
    imports = commands.add_parser("import-openapi", help="import a public OpenAPI document")
    imports.add_argument("--service", required=True, help="service key from environments.yaml")
    imports.add_argument("source", help="URL or path of the OpenAPI JSON document")
    args = parser.parse_args(argv)

    if args.command == "import-openapi":
        return _import(args.spec, args.service, args.source)

    try:
        ir = load(args.spec)
    except SpecError as error:
        print("spec is invalid:", *error.problems, sep="\n  ", file=sys.stderr)
        return 2

    if args.command == "dump-ir":
        json.dump(asdict(ir), sys.stdout, indent=2, default=str)
        print()
        return 0

    stale: list[str] = []
    for language in args.lang or emitters.available():
        module = emitters.load(language)
        stale += _sync(args.root, module.OWNED, module.emit(ir), check=args.check)
    if args.check and stale:
        print("generated code is out of date:", *stale, sep="\n  ", file=sys.stderr)
        return 1
    return 0


def _import(spec: Path, service: str, source: str) -> int:
    services = yaml.safe_load((spec / "environments.yaml").read_text())["services"]
    if service not in services:
        print(f"unknown service {service!r}", file=sys.stderr)
        return 2
    target = spec / "services" / f"{service}.yaml"
    existing = yaml.safe_load(target.read_text()) if target.exists() else None
    document = importer.convert(
        importer.fetch(source), service, services[service]["production"], existing
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(document, sort_keys=False, allow_unicode=True, width=100))
    print(f"wrote {target.relative_to(REPO) if target.is_relative_to(REPO) else target}")
    return 0


def _sync(root: Path, owned: list[str], files: dict[str, str], *, check: bool) -> list[str]:
    changed: list[str] = []
    for relative, content in sorted(files.items()):
        path = root / relative
        if path.exists() and path.read_text() == content:
            continue
        changed.append(relative)
        if not check:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
    for pattern in owned:
        for path in sorted(root.glob(pattern)):
            relative = path.relative_to(root).as_posix()
            if not path.is_file() or relative in files or not fnmatch.fnmatch(relative, pattern):
                continue
            changed.append(f"{relative} (removed)")
            if not check:
                path.unlink()
    return changed


if __name__ == "__main__":
    sys.exit(main())

"""Check a release pair and test its installed wheel without checkout source imports.

Run in a disposable virtual environment with pip and pytest installed.
"""

from __future__ import annotations

import argparse
import ast
import email
import json
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = {".meta", ".env", ".venv", ".git", "__pycache__", ".cache", ".pytest_cache", ".mypy_cache", ".ruff_cache"}


def version() -> str:
    tree = ast.parse((ROOT / "src/exregex/_version.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets):
            value = ast.literal_eval(node.value)
            if isinstance(value, str):
                return value
    raise ValueError("version must be a literal string")


def safe_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or ".." in path.parts or "\\" in name or ":" in name:
        raise ValueError(f"unsafe archive path: {name}")
    if any(p in PRIVATE or p.startswith(".env") or p.endswith((".pyc", ".pyo")) for p in path.parts):
        raise ValueError(f"private/generated archive path: {name}")
    if path.name == "decisions.jsonl" or path.name.endswith(".semantic-v1.jsonl"):
        raise ValueError(f"raw recording archive path: {name}")
    return path


def check(directory: Path, tag: str | None = None) -> tuple[Path, Path, str]:
    current = version()
    if tag is not None and tag != f"v{current}":
        raise ValueError(f"tag {tag!r} must be v{current}")
    wheel = directory / f"ex_regex-{current}-py3-none-any.whl"
    sdist = directory / f"ex_regex-{current}.tar.gz"
    if set(directory.iterdir()) != {wheel, sdist}:
        raise ValueError("release directory must contain exactly the current wheel and sdist")
    prefix = f"ex_regex-{current}"
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        if len(set(names)) != len(names):
            raise ValueError("duplicate wheel paths")
        for name in names:
            path = safe_path(name)
            if stat.S_ISLNK(archive.getinfo(name).external_attr >> 16):
                raise ValueError(f"wheel contains a symlink: {name}")
            if path.parts[0] not in {"exregex", f"{prefix}.dist-info"}:
                raise ValueError(f"unexpected wheel path: {name}")
        metadata = email.message_from_bytes(archive.read(f"{prefix}.dist-info/METADATA"))
        if metadata["Name"] != "ex-regex" or metadata["Version"] != current or metadata.get_all("Requires-Dist"):
            raise ValueError("unexpected name, version or runtime dependencies")
        if "exregex/py.typed" not in names:
            raise ValueError("missing typing marker")
        if f"{prefix}.dist-info/licenses/LICENSE" not in names:
            raise ValueError("missing wheel license")
        wheel_sources = {n: archive.read(n) for n in names if n.startswith("exregex/") and n.endswith(".py")}
        expected = {p.relative_to(ROOT / "src").as_posix(): p.read_bytes() for p in (ROOT / "src/exregex").rglob("*.py")}
        if wheel_sources != expected:
            raise ValueError("wheel code does not match source")
        for content in wheel_sources.values():
            ast.parse(content, feature_version=(3, 10))
    with tarfile.open(sdist) as archive:
        members = archive.getmembers()
        if len({m.name for m in members}) != len(members):
            raise ValueError("duplicate sdist paths")
        files = {}
        for member in members:
            path = safe_path(member.name)
            if path.parts[0] != prefix or not (member.isfile() or member.isdir()):
                raise ValueError(f"unexpected sdist member: {member.name}")
            if member.isfile():
                files[path.relative_to(prefix).as_posix()] = archive.extractfile(member).read()
        for required in ("README.md", "CHANGELOG.md", "LICENSE", "pyproject.toml", "scripts/release.py", "tests/conftest.py"):
            if required not in files:
                raise ValueError(f"missing sdist file: {required}")
        metadata = email.message_from_bytes(files["PKG-INFO"])
        if metadata["Name"] != "ex-regex" or metadata["Version"] != current:
            raise ValueError("sdist metadata does not match release")
        if {n.removeprefix("src/"): data for n, data in files.items() if n.startswith("src/") and n.endswith(".py")} != wheel_sources:
            raise ValueError("sdist code differs from wheel")
        for path in (ROOT / "tests").rglob("*.py"):
            if files.get(path.relative_to(ROOT).as_posix()) != path.read_bytes():
                raise ValueError(f"missing or stale packaged test: {path.name}")
    print(json.dumps({"version": current, "wheel_sources": len(wheel_sources), "sdist_files": len(files)}), flush=True)
    return wheel, sdist, current


def test(directory: Path, tag: str | None = None) -> None:
    wheel, sdist, current = check(directory, tag)
    subprocess.run([sys.executable, "-m", "pip", "install", "--no-index", "--no-deps", "--force-reinstall", str(wheel)], check=True)
    subprocess.run([sys.executable, "-m", "pip", "check"], check=True)
    with tempfile.TemporaryDirectory(prefix="exregex-release-") as scratch:
        root = Path(scratch).resolve()
        with tarfile.open(sdist) as archive:
            for member in archive.getmembers():
                path = safe_path(member.name)
                relative = path.parts[1:]
                # No src/ exists here: eval helpers cannot silently import checkout code.
                if not relative or relative[0] == "src" or member.isdir():
                    continue
                if not member.isfile():
                    raise ValueError("only ordinary files can be extracted")
                target = root.joinpath(*relative).resolve()
                if not target.is_relative_to(root):
                    raise ValueError("archive escapes test directory")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.extractfile(member).read())
        probe = (
            "import exregex, pathlib, sysconfig; "
            "p=pathlib.Path(exregex.__file__).resolve(); "
            "assert p.is_relative_to(pathlib.Path(sysconfig.get_path('purelib')).resolve()), p; "
            f"assert exregex.__version__ == {current!r}; print(p)"
        )
        subprocess.run([sys.executable, "-I", "-c", probe], cwd=root, check=True)
        subprocess.run([sys.executable, "-I", "-m", "pytest", "-q"], cwd=root, check=True)
        subprocess.run([sys.executable, "-I", "-m", "exregex", "--help"], cwd=root, check=True, stdout=subprocess.DEVNULL)
        subprocess.run([sys.executable, "examples/support/demo.py"], cwd=root, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "test"))
    parser.add_argument("directory", type=Path)
    parser.add_argument("--tag")
    args = parser.parse_args()
    directory = args.directory.resolve()
    if args.command == "test":
        test(directory, args.tag)
    else:
        check(directory, args.tag)


if __name__ == "__main__":
    main()

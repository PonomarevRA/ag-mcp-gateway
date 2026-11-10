"""Build a self-contained macOS archive from the public gateway checkout."""

import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import tomllib

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_WIKI = (
    "Home.md", "Home-ru.md", "Installation.md", "Installation-ru.md",
    "Architecture.md", "Architecture-ru.md", "Agents.md", "Agents-ru.md",
    "Operations.md", "Operations-ru.md", "Reference-ru.md", "_Sidebar.md",
)
_EXAMPLES = (
    "company.example.json", "company-basic.example.json", "company-test.example.json",
    "company-prod.example.json", "local-ag-tasks.example.json",
)


def stage_public_guides(bundle: Path, version: str, architecture: str):
    """Install the public guide and synthetic examples alongside the executable."""
    shutil.copy2(ROOT / "README.md", bundle)
    wiki_path = Path("docs", "public", "wiki")
    (bundle / wiki_path).mkdir(parents=True)
    for name in PUBLIC_WIKI:
        shutil.copy2(ROOT / wiki_path / name, bundle / wiki_path / name)
    profiles = bundle / "profiles"
    profiles.mkdir()
    for name in _EXAMPLES:
        shutil.copy2(ROOT / "profiles" / name, profiles)
    metadata = {"version": version, "platform": "macos", "architecture": architecture}
    (bundle / "BUILD-INFO.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")


def build(output: Path) -> Path:
    """Freeze, stage and archive with macOS symlinks intact."""
    if platform.system() != "Darwin":
        raise SystemExit("The standalone archive builder requires macOS")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    version, architecture = project["version"], platform.machine()
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"ag-mcp-gateway-{version}-macos-{architecture}.zip"
    if archive.exists() or archive.is_symlink():
        raise SystemExit(f"Archive already exists: {archive}")
    with TemporaryDirectory(prefix=".amg-build-", dir=output) as scratch:
        work = Path(scratch)
        subprocess.run([
            sys.executable, "-m", "PyInstaller", "--noconfirm", "--workpath", str(work / "work"),
            "--distpath", str(work / "dist"), str(ROOT / "ag-mcp-gateway.spec"),
        ], cwd=ROOT, check=True)
        bundle = work / "dist" / "ag-mcp-gateway"
        stage_public_guides(bundle, version, architecture)
        if subprocess.check_output([bundle / "ag-mcp-gateway", "--version"], text=True).strip() != version:
            raise SystemExit("Frozen executable and package versions differ")
        staged_archive = work / archive.name
        subprocess.run(["/usr/bin/ditto", "-c", "-k", "--keepParent", bundle, staged_archive], check=True)
        # Publish in one step without overwriting an archive built concurrently.
        os.link(staged_archive, archive)
    return archive


def main():
    """Accept a destination and print the finished archive path."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    print(build(args.output))


if __name__ == "__main__":
    main()

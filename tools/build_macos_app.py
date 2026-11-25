"""Build an AppKit launcher beside the gateway CLI in a standalone bundle."""

from pathlib import Path
import plistlib
import struct
import subprocess
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
APP_NAME = "ag-mcp-gateway.app"
IDENTIFIER = "local.ag-mcp-gateway.desktop"


def build_app(bundle: Path, version: str, architecture: str) -> Path:
    """Compile, decorate and ad-hoc sign the macOS application bundle."""
    app = bundle / APP_NAME
    contents = app / "Contents"
    macos = contents / "MacOS"
    resources = contents / "Resources"
    macos.mkdir(parents=True)
    resources.mkdir()
    executable = macos / "GatewayApp"
    sdk = subprocess.check_output(["/usr/bin/xcrun", "--show-sdk-path"], text=True).strip()
    sources = [ROOT / "desktop" / name for name in ("GatewayApp.swift", "GatewayMark.swift", "ProfileForm.swift")]
    with TemporaryDirectory(prefix="amg-app-") as scratch:
        work = Path(scratch)
        subprocess.run(["/usr/bin/xcrun", "swiftc", "-O", "-swift-version", "5",
                        "-target", f"{architecture}-apple-macos12.0", "-sdk", sdk,
                        "-module-cache-path", str(work / "cache"), *sources, "-o", executable], check=True)
        write_icon(executable, resources, work)
    metadata = {
        "CFBundleIdentifier": IDENTIFIER,
        "CFBundleName": "ag-mcp-gateway",
        "CFBundleDisplayName": "ag-mcp-gateway",
        "CFBundleExecutable": executable.name,
        "CFBundleIconFile": "Gateway.icns",
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": version,
        "CFBundleVersion": version,
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
        "CFBundleURLTypes": [{"CFBundleURLName": IDENTIFIER, "CFBundleURLSchemes": ["agm-gateway"],
                              "CFBundleTypeRole": "Viewer"}],
    }
    (contents / "Info.plist").write_bytes(plistlib.dumps(metadata))
    subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", app], check=True)
    return app


def write_icon(executable: Path, resources: Path, work: Path) -> None:
    """Generate all required representations from the same native mark."""
    entries = bytearray()
    for kind, size in ((b"icp4", 16), (b"icp5", 32), (b"icp6", 64), (b"ic07", 128),
                       (b"ic08", 256), (b"ic09", 512), (b"ic10", 1024)):
        image = work / f"{size}.png"
        subprocess.run([executable, "--render-icon", image, str(size)], check=True)
        data = image.read_bytes()
        entries.extend(struct.pack(">4sI", kind, len(data) + 8))
        entries.extend(data)
    (resources / "Gateway.icns").write_bytes(struct.pack(">4sI", b"icns", len(entries) + 8) + entries)

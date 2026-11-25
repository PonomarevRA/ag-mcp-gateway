# Install and upgrade

[Русский](Installation-ru.md) · [Wiki home](Home.md)

## Standalone on macOS

Download the archive for your machine from the approved distribution channel. Check `uname -m` and verify the checksum with `shasum -a 256 <archive.zip>`. An x86-64 archive requires Rosetta on Apple Silicon. The binary and its `_internal` directory must stay together. No Python, Node.js or administrator installation is required for the extracted build.

Extract into a fresh directory, for example with `/usr/bin/ditto -x -k archive.zip ./gateway-release`. Then run:

```sh
cd ./gateway-release/ag-mcp-gateway
./ag-mcp-gateway --version
./ag-mcp-gateway demo-profile
./ag-mcp-gateway serve --allow-unauthenticated
```

The demo creates a synthetic local MCP. The overview is at [127.0.0.1:18792](http://127.0.0.1:18792/). Run `./ag-mcp-gateway status` from another terminal. Reuse the generated profile on subsequent starts. If the demo already exists, `demo-profile` reports an existing file.

## Manage profiles in the macOS window

Open `ag-mcp-gateway.app` from the extracted directory and keep it beside the CLI. Choose **+ → Create profile** in the window toolbar. Set the name and environment, then add the first MCP server. The gateway generates a separate profile token in macOS Keychain. Select the profile and choose **Start**. To add a server, choose **MCP catalog → Add MCP**. If the app started the profile, confirm a restart to apply the saved configuration.

Use the menu bar item to open the window or select a profile. Launching the app again brings its existing window to the front. Profiles keep running when you close the window or quit the app. Choose **File → Stop profile** to stop a service.

## Working profiles and updates

Profile JSON lives under `~/.ag-mcp-gateway/profiles` by default. Copy a bundled `profiles/*.example.json` to an actual name such as `company-test.json`. Match the JSON `name` to the filename. Set approved MCP endpoints, symbolic secret references and tool policies. Pass `--profile company-test` before the command. Use `--profiles`, `--state` and `--port` before the command for explicit isolation.

Start a protected profile with `./ag-mcp-gateway --profile company-test serve`. The default ports are 18792 for an unlabeled profile, 18793 for test and 18794 for production. Two simultaneous profiles require distinct ports and state identities. Production requires a token. Browser login uses `gateway` and the profile token as the password.

On macOS, a protected profile can start when an MCP client connects. Generate the Codex entry with `./ag-mcp-gateway --profile company-test config --start-if-stopped codex`. The `--start-if-stopped` flag after `stdio` starts the service on demand. The service keeps running after the client exits.

For an upgrade, stop the old process with Ctrl+C, keep the profile and state directories, and extract the new build into a fresh directory. Start the new executable against the same profile. Regenerate client entries if the binary path changed. Keep the preceding build and a policy-compliant backup of local state for rollback. Validate compatible state before reverting to an older build.

## Build the macOS archive

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[build,test]'
python -m pytest -q
python tools/build_standalone.py --output dist
```

Build with the intended interpreter architecture. The archive contains the executable, its runtime, README, wiki, example profiles and build metadata. For source-mode testing, use `python -m gateway --help`. Only report platform qualification for a build tested on that platform.

For a separate GitHub Wiki, run `python tools/export_wiki.py /path/to/new-wiki-export` from the checkout. The output contains public pages with Wiki links. It can be reviewed before importing into an initialized Wiki.

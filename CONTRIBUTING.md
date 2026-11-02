# Contributing

获客平台 is a local acquisition research and evidence-review tool. The native
desktop interface, CLI and MCP client share an authenticated loopback HTTP API.
Suggestions are candidates for review, not confirmed customers or orders.

## Development setup

Use Python 3.12 and work from the repository root, which contains
`leadworkbench/`, `tests/`, `static/` and `certs/`.

The business backend and its unittest suite use the Python standard library.
They do not require Qt, a browser account, a model server or OpenCLI:

```text
python -B -S -m unittest discover -s tests -v
```

The tests use temporary data directories, synthetic evidence, mocked collectors
and temporary loopback HTTP fixtures. A model-shaped HTTP fixture is not a real
model integration test. Keep normal CI independent of personal accounts,
external platforms and installed model controllers.

For native desktop work, create a dedicated virtual environment and install the
pinned runtime:

```text
python -m venv .venv
```

On Windows PowerShell:

```powershell
& .\.venv\Scripts\python.exe -m pip install --index-url https://pypi.org/simple -r requirements-desktop.txt
& .\.venv\Scripts\python.exe -m leadworkbench.launcher desktop --data-dir .\dev-data
```

The desktop uses PySide6 QtWidgets. Do not introduce a WebView or QtWebEngine
wrapper. The application connects to a model service that the user starts; it
does not start, stop or reconfigure a model controller. Choose a real endpoint
and the model ID advertised by that service when testing optional inference;
an example model ID is not a bundled model.

## Change boundaries

Read `AGENTS.md` and `API_CONTRACT.json` before changing integrations. Use the
shared HTTP API for UI, CLI and MCP business operations. Preserve immutable
source evidence, explicit demo/partial flags, candidate versions, review
idempotency and conflict checks.

Keep network access, file parsing and other blocking operations off the Qt GUI
thread. Source text is untrusted data and must remain plain text; it cannot
authorize tool execution, messages or changes to local settings.

New collector support must be bounded and read-only. Use synthetic or sanitized
fixtures for posts and comments. Do not use real prospect messages, production
deletion, authenticated browser profiles or real model calls as an ordinary
test requirement.

## Pull requests

Describe the problem, resulting behavior and checks you actually ran. Separate
fixture results, source inspection and live acceptance. Include a screenshot
for a visual change and explain any platform-specific limitations. Do not
claim an untested operating system, account integration or model configuration
is supported merely because a mock test passes.

Keep unrelated changes separate. Do not commit runtime data, SQLite databases,
model settings, authentication tokens, browser profiles, imported evidence,
generated executables or personal machine paths. Use synthetic examples and
an explicit development data directory.

The default CI runs backend tests on Windows with Python 3.12 and no third-party
Python packages. A manual `workflow_dispatch` run can enable the optional Qt
import/compile check. This check does not open the app, collect data or test
model generation. Ubuntu backend execution can be added after a real Linux CI
run establishes the result; Windows packaging is a separate task.

## Windows packaging

Use the Windows build script in a dedicated environment. Install the desktop
requirements and a compatible PyInstaller version; the current packaging
baseline is PyInstaller 6.22.3:

```powershell
& .\.venv\Scripts\python.exe -m pip install PyInstaller==6.22.3
.\build.ps1 -Python .\.venv\Scripts\python.exe -DesktopLauncher
```

The complete desktop distribution uses the default `onedir` layout. Keep the
`LeadWorkbench` and `LeadWorkbenchDesktop` directories together and retain their
resources and notices. `-OneFile` is not a complete desktop distribution and
must not be combined with `-DesktopLauncher`. Build Windows executables on
Windows. Do not add automatic releases, signing or service deployment to normal
pull-request CI.

Report vulnerabilities using [SECURITY.md](SECURITY.md), rather than posting
sensitive details in an ordinary issue.

# Changelog

Este arquivo registra as melhorias entregues em cada commit. As versões seguem
o formato [SemVer](https://semver.org/): `MAJOR.MINOR.PATCH`. A identidade
exibida no produto é `RemoteOps <versão> (Build <número>)`.

## [Unreleased]

Use esta seção para registrar a próxima melhoria antes de criar o commit.

## [1.8.0] (Build 80) - 2026-09-04 a 2026-09-16

Identidade: **RemoteOps 1.8.0 (Build 80)**.

- Return complete inventory JSON via remote result file (`ADMIN$`/`C$`) and Base64 so Hardware, Network, Video and other inventory queries no longer fail with truncated stdout (`Resposta não é JSON válido`) in the windowed `.exe`.
- Show connected monitor manufacturer, model and serial in the Video inventory section (`WmiMonitorID`).
- Bind remote inventory results to the originating request (host, section and request id) so a stale query cannot display or cache another computer's data after a host change.
- Fix local-account listing in the packaged `.exe` (not only Guest, no `Connecting to…` dialog): `Get-LocalUser` with CIM fallback, JSON via `ADMIN$`/`C$` and Base64, ignore PsExec status lines.
- Emit only `RemoteOps-<versão>-Build<n>.exe` from the PyInstaller build (do not create `dist/RemoteOps.exe`).
- Detect the versioned exe in `build.bat` without a nested `python -c` (broken under `cmd.exe`).

- 2026-09-16 · Fix truncated inventory JSON from remote PowerShell and list monitors in Video.
- 2026-09-12 · Inventory request identity: reject stale results, errors and worker completion after host or request changes.
- 2026-09-04 · `b783f8d` · Fix packaged exe still returning only the Guest local account.
- 2026-09-04 · `e4220a3` · Emit only the versioned executable from the PyInstaller build.
- 2026-09-04 · `00bc8a0` · Document versioned PyInstaller output in the changelog.
- 2026-09-04 · `437cc4e` · Fix versioned exe detection in `build.bat` under `cmd.exe`.

## [1.7.0] (Build 75) - 2026-09-04

Identidade: **RemoteOps 1.7.0 (Build 75)**.

- Fix remote local-account queries in the packaged `.exe` (`Resposta não é JSON válido`).
- Run remote PowerShell via `-EncodedCommand` with UTF-8 stdout so windowed builds match `python main.py`.

- 2026-09-04 · `e07bab5` · Fix remote PowerShell JSON queries in the windowed executable.
- 2026-09-04 · `d2cbd10` · Return all remote local accounts from the packaged executable.

## [1.6.0] (Build 74) - 2026-08-10 a 2026-09-04

Identidade: **RemoteOps 1.6.0 (Build 74)**.

- Add a changelog to track improvements by commit.
- Document the SemVer-based application versioning workflow.
- Display the application version and build in the window title.
- Update packaging and build configuration.

- 2026-09-04 · `532ec8e` · Document RemoteOps 1.6.0 (Build 74) for the GitHub release.
- 2026-09-04 · `3f84a48` · Display application version and build.
- 2026-09-04 · `b2f549d` · Add changelog and document versioning.
- 2026-09-03 · `b851cec` · Update packaging and build configuration.

## [1.5.0] - 2026-09-04

Identidade: **RemoteOps 1.5.0**.

- Add the final remote power and printer management releases.

- 2026-09-04 · `0faa6c1` · Add remote power controls.
- 2026-09-04 · `e020d25` · Add remote printer management.

## [1.4.0] - 2026-09-01 a 2026-09-03

Identidade: **RemoteOps 1.4.0**.

- Add remote inventory management, file sessions and logged-on user views.
- Add batch application installation.
- Add host connectivity diagnostics.

- 2026-09-03 · `3789e3f`, `24b7f9e` · Add host connectivity diagnostics and batch application installation.
- 2026-09-02 · `3665248`, `c713916`, `07263e7`, `98b06cf`, `f203544` · Refine the UI, improve search, add session views and batch installation.
- 2026-09-01 · `b2a7726`, `3cec145`, `e832fac` · Add inventory, file-session and logged-on user views.

## [1.3.0] - 2026-08-24 a 2026-08-31

Identidade: **RemoteOps 1.3.0**.

- Add remote power controls, printer management, messaging support and host connectivity diagnostics.
- Add and update automated tests.

- 2026-08-31 · `525b471`, `820db59` · Add remote power controls.
- 2026-08-28 · `64a3bbd`, `37a51ad` · Add host connectivity diagnostics.
- 2026-08-27 a 2026-08-25 · `416cb6f`, `6dff07f`, `a421dc4`, `a19ee4f`, `7668940`, `ab2bf53`, `4f0bcd9`, `fcfa643`, `5a59431` · Add remote printer management and improve PsExec handling.
- 2026-08-25 a 2026-08-24 · `86ecbd7`, `8c2cad3` · Add remote messaging support.
- 2026-08-25 · `211e169`, `9c279f2` · Add or update automated tests.
- 2026-08-25 · `857a0b0` · Update RemoteOps.

## [1.2.0] - 2026-08-18 a 2026-08-23

Identidade: **RemoteOps 1.2.0**.

- Improve PsExec, command prompt, WinGet and Robocopy execution.
- Improve remote operations infrastructure and application search.
- Refine the desktop user interface and update project documentation.

- 2026-08-23 · `e8739ea` · Improve WinGet remote execution.
- 2026-08-21 · `56513d5`, `77ac4ab`, `90b6bff`, `a640581`, `ec3ca79`, `b97fb0c` · Improve execution, documentation, UI, WinGet and batch installation.
- 2026-08-20 · `68d74ce`, `72a3015`, `51e03b6`, `01ec398` · Improve infrastructure, tests and batch installation.
- 2026-08-19 · `a91aedd`, `834288a`, `905406e`, `0ce84cf` · Improve search, infrastructure and WinGet execution.
- 2026-08-18 · `f098447`, `4df87f1`, `3e4be7b`, `2633eba`, `fe9f192`, `f3af816`, `2a63b48`, `9d975da` · Refine the UI and improve WinGet, PsExec, command prompt and Robocopy execution.
- 2026-08-17 · `417184d` · Improve PsExec command handling.

## [1.1.0] - 2026-08-11 a 2026-08-13

Identidade: **RemoteOps 1.1.0**.

- Improve WinGet remote execution and application search.
- Refine the desktop user interface and add automated tests.

- 2026-08-13 · `863bc17`, `f0d903c`, `dfdf95c` · Improve application search and add/update tests.
- 2026-08-12 · `483757c`, `bfa2654`, `39beada`, `c079c17`, `f281319` · Improve WinGet remote execution.
- 2026-08-12 · `db978e4`, `19e94c1`, `92bc127` · Improve application search.
- 2026-08-12 · `1e05579` · Refine the desktop user interface.
- 2026-08-11 · `a7cdddd` · Refine the desktop user interface.

## [1.0.0] - 2026-08-10

Identidade: **RemoteOps 1.0.0**.

- Initialize the RemoteOps application with its desktop interface, remote execution, PsExec, PowerShell, WinGet, application search, inventory, Robocopy and settings foundations.
- Add initial project documentation, packaging configuration and branding assets.

- 2026-08-10 · `58999ba` · Improve PsExec command handling.
- 2026-08-10 · `1a231fc` · Update project documentation.
- 2026-08-10 · `eb44775` · Update RemoteOps.
- 2026-08-10 · `dd14acc` · Refine the desktop user interface.
- 2026-08-10 · `b5e501e` · Initialize RemoteOps application.

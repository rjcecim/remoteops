# Changelog

Este arquivo registra as melhorias entregues em cada commit. As versões seguem
o formato [SemVer](https://semver.org/): `MAJOR.MINOR.PATCH`. A identidade
exibida no produto é `RemoteOps <versão> (Build <número>)`.

## [1.13.0] (Build 93) - 2026-10-15

Identidade: **RemoteOps 1.13.0 (Build 93)**.

- Harden the Robocopy tab: validate the remote C: destination (reject UNC, other drives, `..`, invalid characters and reserved names), show UNC feedback under the field, and keep the two cards Destino and Parâmetros.
- Add explicit `/R` and `/W` controls (defaults 2 and 3) with the seven quiet switches; reset restores all of them. Build argv from structured params only — never `/MIR`, `/PURGE`, `/MOVE` or link-follow switches.
- Fail closed when a required Robocopy step is invalid: structured `robocopy_errors` block the frozen plan so PsExec cannot run without a valid copy. Folder selection with an `.exe` enables Robocopy for dependencies; a lone `.exe` still uses PsExec `-c`.
- Classify Robocopy exit codes (0–7 vs ≥8), log the interpretation, and refuse the PsExec follow-up after cancel or copy failure. MSI batch uses the same destination and error metadata.

- 2026-10-15 · Harden Robocopy destination validation, `/R`/`/W`, fail-closed copy→PsExec plans and folder+EXE transfers.

## [1.12.0] (Build 92) - 2026-10-15

Identidade: **RemoteOps 1.12.0 (Build 92)**.

- Rebuild the PsExec Desempenho card around the destination computer. Priority maps to `-low`, `-belownormal`, `-abovenormal`, `-high`, `-realtime` and `-background`. CPU group and affinity use that computer's processor groups.
- Query the groups with PsExec as the configured account, off the UI thread, and read the result from a temporary file when the console pipe comes back empty. Refresh uses the same icon button as the session list.
- Keep the affinity text as typed. A bad format, a duplicate or an index outside a known group blocks the command. A well-formed value whose limits were not verified stays in the command with a warning. Tempo real asks for confirmation before the TCP 445 check.
- Drop the previous limits when the host or the credential changes. A group that is no longer present is not replaced by Automático.
- Restore the main window to the size it had before Autenticação or Desempenho expanded it, when those cards are collapsed again.
- Parse program arguments with Windows `CommandLineToArgvW` rules shared by PsExec extras, Robocopy switches and msiexec tokens. Validate connection timeout `-n` from 0 to 9999.
- Treat `-d` as a detached launch: a local PID is not reported as remote failure, and the log states that the remote process is not monitored.
- Freeze each Executar attempt as a validated plan so a TCP 445 precheck cannot launch a command rebuilt from later edits. Run/Stop follow an explicit phase, and late or cancelled precheck callbacks do not start execution. Rename the preview checkbox to “Abrir em console externo”.
- Harden the Console de Saída: incremental UTF-8/ANSI, carriage-return progress, bounded ConPTY buffers and visual retention, smart scroll, and a redaction gateway shared by every `LogOutputWidget` (including WinGet and other tabs).

- 2026-10-15 · Harden console streams, ConPTY retention and redaction across all output consoles.
- 2026-10-15 · Freeze validated Executar attempts through TCP precheck and cancel safely.
- 2026-10-15 · Harden PsExec flags, Windows argument parsing and detached `-d` results.
- 2026-10-15 · Restore the window size after collapsing Autenticação and Desempenho.
- 2026-10-15 · Validate PsExec priority, CPU group and affinity against the remote computer.

## [1.11.0] (Build 87) - 2026-10-15

Identidade: **RemoteOps 1.11.0 (Build 87)**.

- Replace the PsExec authentication form with three credential modes. Windows atual sends no `-u` or `-p`. Domínio sends `DOMÍNIO\usuário` and fills the domain from the Windows session, then from the remote computer when that lookup is a real domain. Conta local sends `.\usuário` for the remote SAM account.
- Keep a domain prefix the operator typed. A workgroup or computer name is not used as the account domain. Switching mode clears the account and password.
- Mask the password, with a show/hide control that never puts it in the tooltip, preview, history or log. Spaces and symbols are preserved. An empty password with a username still omits `-p` so PsExec can prompt.
- Replace the elevation checkboxes with one privilege level: Padrão, Elevado (`-h`), SYSTEM (`-s`) or Limitado (`-l`). Shell commands and executable preselect still choose SYSTEM, and the control shows that effective flag. `-e` remains incompatible with `-s`.
- Choose an interactive session with `-i` or `-i N`. List remote sessions as returned, refresh on demand, and ignore a result whose host, account or password no longer matches the request. A session that disappeared must be chosen again before an interactive run.

- 2026-10-15 · Add credential modes and reliable privilege and session selection for PsExec.

## [1.10.0] (Build 86) - 2026-09-23 a 2026-10-15

Identidade: **RemoteOps 1.10.0 (Build 86)**.

- Add a dedicated Instalação em Lote tab for MSI (same title as the EXE batch tab). After the scan it shows the same results table as EXE — computer, app, version, desired version, action, result and reason. Installation runs one computer at a time: Robocopy first, then `msiexec` through PsExec. The MSI tab remains the parameter form.
- Report PsExec host status as network connectivity. TCP 445 open is no longer labeled as PsExec authorization. ICMP blocked with TCP 445 open stays reachable; a failed check is not shown as offline.
- Drop stale connectivity results when the host changes, cancel checks when the window closes, and recheck on demand without reusing the short cache.
- Reject malformed hosts before probing, and pass IPv6 literals to PsExec as a bracketed UNC target.

- 2026-10-15 · Correct PsExec connection status, host checks and worker lifetime.
- 2026-09-23 · Add MSI batch install with version scan, Robocopy then msiexec, one host at a time.

## [1.9.0] (Build 84) - 2026-09-17 a 2026-09-22

Identidade: **RemoteOps 1.9.0 (Build 84)**.

- Add a standalone Host Search tab, independent of Application Search, opened from a header host button (`E968`).
- Scan the active Settings IP range over TCP 445/135/139 (NetBIOS then DNS; ICMP does not exclude the host) with no `hosts.json` fallback.
- List IP, hostname, active WTS user (`DOMAIN\user` via the same sessions/IPC$ backend as Power/Message), domain full name (`NetUserGetInfo`, cached, no `net.exe`) and an eye action that fills PsExec and focuses the host field.
- Filter discovered rows live by IP, hostname, user or full name, without restarting the scan.
- Show active users whose login is numeric: parse qwinsta/quser without treating the account as a session id, fall back when WTS omits the username, accept Portuguese `Ativo`, and query the hostname before the IP.
- Correct Inventory Overview and System on the selected host: identify Windows 10/11 from `Win32_OperatingSystem.Caption` (not PsInfo Product type), convert `TotalPhysicalMemory` from bytes once, show version/build/architecture/user/network details, and reject data from another computer.

- 2026-09-22 · Correct inventory Overview and System OS/memory from CIM instead of stale PsInfo fields.
- 2026-09-18 · Show Host Search full names from AD via NetUserGetInfo (no net.exe).
- 2026-09-17 · Fix Host Search omitting active users whose login is numeric.
- 2026-09-17 · Add standalone Host Search with active-user lookup and PsExec handoff.

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

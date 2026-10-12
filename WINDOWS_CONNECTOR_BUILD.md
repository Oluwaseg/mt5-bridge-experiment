# Profit Pilot Connector: Windows Build

This produces an unsigned standalone Windows executable for development evaluation. It is not a signed installer or a production release.

## Requirements

- Windows 10 or 11 x64
- Python 3.11 x64
- A Windows-compatible `MetaTrader5` wheel matching the pinned version in `requirements-mt5-windows.txt`
- Internet access for installing the build dependencies

The MetaTrader5 Python package is Windows-specific. Build and smoke-test this bundle on Windows; a Linux/Wine source test does not prove that the native terminal API or packaged executable works.

## Reproducible build

From PowerShell in this folder:

```powershell
py -3.11 -m venv .venv-connector
.\.venv-connector\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r .\requirements-connector-build.lock.txt
python -m unittest discover -s .\bridge -p test_connector.py
python -m unittest discover -s .\bridge -p test_worker_validation.py
python -m PyInstaller --clean --noconfirm .\profit_pilot_connector.spec
.\dist\ProfitPilotConnector.exe --smoke-test-ui
.\dist\ProfitPilotConnector.exe --smoke-test-bundle
```

The UI smoke renders the packaged Tk window without loading credentials or starting workers. The bundle smoke imports the packaged MT5/NumPy modules, starts the bundled loopback probe, and exercises the existing worker identify/ping/heartbeat messages against a local mock backend. Neither smoke test logs in to an MT5 account or executes trades.

Expected output is `dist\ProfitPilotConnector.exe`. The first launch should be tested on a clean Windows account with MT5 installed, and pairing should be tested only against a non-production account until the package has passed the release checklist below.

## Release checks still required

- Confirm PyInstaller collects the MetaTrader5 native modules and terminal dependencies on clean Windows machines.
- Verify DPAPI credential save/load across application restarts and Windows user sessions.
- Verify terminal discovery for common broker installs and the manual browse path.
- Verify start, stop, reconnect, account/server mismatch, offline terminal, code expiry and revocation behavior.
- Sign the executable and installer, establish a trusted update channel and rollback process, and publish checksums.
- Package an installer (for example, WiX Toolset or Inno Setup) only after the executable is verified. No installer is built by this spec.

The app's production API base URL is defined in `bridge/connector_core.py`; change that constant through the release process when the production backend origin changes. `PROFIT_PILOT_API_URL` may override it for development. No account-specific values belong in the build or environment.

The lock file records the exact dependency versions used for the verified Python 3.11.9 Windows x64 build. For a controlled development backend override, set `PROFIT_PILOT_API_URL` before launching the source app; only loopback may use HTTP. Remote backends must use HTTPS, and the worker URL is derived as WSS at `/api/worker-connect`.

## Rebuild the Existing Project EXE

Use this PowerShell procedure to replace the executable in the project's `dist` folder. It checks that the connector is closed normally, makes and verifies a backup outside `dist`, builds from the current source into the project's actual `dist`, and runs both smoke checks against that output. It never terminates a running process or deletes the backup. If the build or either smoke check fails, it restores the prior executable; the restored file is the old build and must not be treated as a newly verified executable.

The absolute paths below are specific to the computer and Windows user used for this build. Your PC name, Windows user name (the folder under `C:\Users`), project location, temporary directory, or virtual-environment location may be different. Before running the commands, update `$Project`, `$Python`, and `$BackupDir` to paths that exist on your machine; keep the backup outside the project's `dist` folder.

```powershell
$Project = 'C:\Users\HomePC\Desktop\Projects\trading\mt5-bridge-experiment'
$Exe = Join-Path $Project 'dist\ProfitPilotConnector.exe'
$Python = 'C:\Users\HomePC\AppData\Local\Temp\profit-pilot-connector-build-venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $Exe)) {
	throw "Existing executable not found: $Exe"
}
if (-not (Test-Path -LiteralPath $Python)) {
	throw "Pinned Python 3.11 build environment not found: $Python"
}

$Running = Get-CimInstance Win32_Process |
	Where-Object { $_.ExecutablePath -eq $Exe }
if ($Running) {
	$Running | Select-Object ProcessId, Name, ExecutablePath | Format-Table -AutoSize
	throw 'Close the Profit Pilot Connector windows normally, then rerun this procedure.'
}

$BackupDir = 'C:\Users\HomePC\AppData\Local\Temp\profit-pilot-connector-build\backup'
New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null
$Backup = Join-Path $BackupDir "ProfitPilotConnector.exe.pre-rebuild-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
Copy-Item -LiteralPath $Exe -Destination $Backup
if ((Get-FileHash -LiteralPath $Exe).Hash -ne (Get-FileHash -LiteralPath $Backup).Hash) {
	throw "Backup verification failed. The existing executable was not removed. Backup: $Backup"
}

Remove-Item -LiteralPath $Exe -Force
try {
	Push-Location $Project
	& $Python -m PyInstaller --clean --noconfirm `
		--distpath (Join-Path $Project 'dist') `
		--workpath "$env:TEMP\profit-pilot-connector-build\project-dist-work" `
		(Join-Path $Project 'profit_pilot_connector.spec')
	if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $Exe)) {
		throw "PyInstaller failed with exit code $LASTEXITCODE."
	}

	& $Exe --smoke-test-ui
	if ($LASTEXITCODE -ne 0) {
		throw "Packaged UI smoke test failed with exit code $LASTEXITCODE."
	}

	& $Exe --smoke-test-bundle
	if ($LASTEXITCODE -ne 0) {
		throw "Packaged bundle smoke test failed with exit code $LASTEXITCODE."
	}
}
catch {
	if (Test-Path -LiteralPath $Backup) {
		Copy-Item -LiteralPath $Backup -Destination $Exe -Force
		Write-Warning "Build or smoke test failed. Restored the prior executable from $Backup; it is not the new build."
	}
	throw
}
finally {
	Pop-Location
}

Get-Item -LiteralPath $Exe | Select-Object FullName, Length, LastWriteTime
Write-Output "Preserved backup: $Backup"
```

```powershell

& $Exe --smoke-test-ui
if ($LASTEXITCODE -ne 0) { throw 'UI smoke test failed.' }

& $Exe --smoke-test-bundle
if ($LASTEXITCODE -ne 0) { throw 'Bundle smoke test failed.' }

```

# Build with: pyinstaller --clean profit_pilot_connector.spec
from PyInstaller.utils.hooks import collect_all

mt5_datas, mt5_binaries, mt5_hidden = collect_all("MetaTrader5")
websocket_datas, websocket_binaries, websocket_hidden = collect_all("websockets")
flask_datas, flask_binaries, flask_hidden = collect_all("flask")
dotenv_datas, dotenv_binaries, dotenv_hidden = collect_all("dotenv")

a = Analysis(
    ["connector_app.py"],
    pathex=["."],
    binaries=mt5_binaries + websocket_binaries + flask_binaries + dotenv_binaries,
    datas=[("bridge/probe.py", "bridge"), ("bridge/worker_identity.py", "bridge")]
    + mt5_datas
    + websocket_datas
    + flask_datas
    + dotenv_datas,
    hiddenimports=mt5_hidden
    + ["numpy", "numpy._core._exceptions"]
    + websocket_hidden
    + flask_hidden
    + dotenv_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ProfitPilotConnector",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)

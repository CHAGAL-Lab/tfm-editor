# PyInstaller spec: build from the project folder with `pyinstaller packaging/tfm-editor.spec` (same on Windows and Linux).
# The result is the single file dist/tfm-editor. Read-only data (the athlete name table and pyproject.toml, which holds the version) is bundled with it.
import os

root = os.path.abspath(os.path.join(SPECPATH, ".."))

a = Analysis(
    [os.path.join(root, "run_gui.py")],
    pathex=[root],
    datas=[(os.path.join(root, "data", "names.json"), "data"), (os.path.join(root, "pyproject.toml"), ".")],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="tfm-editor",
    console=False,      # do not open a console window on Windows (no effect on Linux)
    upx=False,
)

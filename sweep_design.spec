# PyInstaller build spec for Sweep Design.  See BUILD.md for the full recipe.
#
#   pyinstaller sweep_design.spec              -> one folder (recommended)
#   set SD_ONEFILE=1 && pyinstaller sweep_design.spec   -> single .exe
#
# Must be run ON Windows to produce a Windows .exe: PyInstaller freezes the
# interpreter and libraries of the machine it runs on and cannot cross-compile.

import os

ONEFILE = os.environ.get("SD_ONEFILE", "") not in ("", "0")

a = Analysis(
    ["sweep_design.py"],
    pathex=[],
    binaries=[],
    # Shipped inside the bundle so the exe is self-explanatory on a machine
    # that never saw the source. Neither is loaded at runtime.
    datas=[("README.md", "."), ("LICENSE", ".")],
    # sweep_engine is a plain local import and is picked up automatically.
    # matplotlib's TkAgg backend likewise: sweep_design.py imports
    # backend_tkagg by name rather than only through matplotlib.use().
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    # Toolkits and dev tools that numpy/scipy/matplotlib reference but this
    # app never uses. Cuts roughly a third off the bundle. PIL is deliberately
    # NOT excluded -- matplotlib reaches for it on some save paths.
    excludes=[
        "PyQt5", "PyQt6", "PySide2", "PySide6", "wx",
        "IPython", "jupyter", "notebook", "nbformat",
        "pandas", "pytest", "sphinx", "setuptools",
        "matplotlib.backends.backend_webagg",
        "matplotlib.backends.backend_qtagg",
        "matplotlib.backends.backend_wxagg",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

if ONEFILE:
    exe = EXE(
        pyz, a.scripts, a.binaries, a.datas, [],
        name="SweepDesign",
        debug=False,
        strip=False,
        upx=False,          # UPX-packed exes are a reliable way to get
                            # flagged by antivirus; the size win is not
                            # worth the support burden.
        console=False,      # GUI app: no console window behind it.
        icon=None,
    )
else:
    exe = EXE(
        pyz, a.scripts, [],
        exclude_binaries=True,
        name="SweepDesign",
        debug=False,
        strip=False,
        upx=False,
        console=False,
        icon=None,
    )
    coll = COLLECT(
        exe, a.binaries, a.datas,
        strip=False,
        upx=False,
        name="SweepDesign",
    )

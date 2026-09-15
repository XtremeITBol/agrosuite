# -*- mode: python ; coding: utf-8 -*-
"""Spec de PyInstaller para el ejecutable de Windows.

Notas que explican por qué esto no es el spec por defecto:

* scikit-learn y scipy cargan submódulos por nombre en tiempo de ejecución, así
  que el análisis estático de PyInstaller no los detecta. Van en hiddenimports.
* El mismo motivo aplica a los backends de sklearn y a scipy.special._cdflib.
* Se excluyen matplotlib, tkinter, IPython y compañía: no los usa el servidor y
  suman cientos de MB al bundle.
* Se usa modo ONEDIR, no ONEFILE. Un .exe onefile se descomprime entero en
  %TEMP% en CADA arranque — con scipy y sklearn adentro son ~20 segundos de
  espera cada vez, y algunos antivirus lo marcan como sospechoso.
"""
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent

hiddenimports = []
for pkg in ("sklearn", "scipy", "scipy.special", "scipy.optimize",
            "scipy.sparse.csgraph", "sklearn.utils", "sklearn.ensemble",
            "sklearn.neighbors", "sklearn.tree", "sklearn.linear_model",
            "sklearn.feature_extraction", "sklearn.metrics"):
    try:
        hiddenimports += collect_submodules(pkg)
    except Exception:
        pass
hiddenimports += [
    "sklearn.utils._typedefs", "sklearn.utils._heap", "sklearn.utils._sorting",
    "sklearn.utils._vector_sentinel", "sklearn.neighbors._partition_nodes",
    "scipy._lib.messagestream", "scipy.special.cython_special",
    "scipy.optimize._highspy", "joblib", "cryptography",
    "cryptography.hazmat.primitives.asymmetric.rsa",
    "cryptography.hazmat.backends.openssl", "pandas._libs.tslibs.base",
    "engineio.async_drivers.threading",
]

datas = [
    (str(ROOT / "web"), "web"),
]
for pkg in ("sklearn", "scipy"):
    try:
        datas += collect_data_files(pkg)
    except Exception:
        pass

excludes = [
    "matplotlib", "tkinter", "IPython", "notebook", "jupyter", "pytest",
    "PyQt5", "PyQt6", "PySide2", "PySide6", "wx", "sphinx", "docutils",
    "PIL.ImageQt", "sklearn.externals.array_api_compat.torch",
    # "unittest" NO va acá: numpy.testing lo importa en tiempo de carga (vía
    # scipy._external.array_api_compat), y ese import ocurre igual aunque la
    # app nunca corra pruebas. Excluirlo hacía crashear el .exe con
    # "ModuleNotFoundError: No module named 'unittest'" al importar sklearn.
    "test", "pydoc_data", "lib2to3",
]

a = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AgroSuite",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,               # UPX dispara falsos positivos de antivirus
    console=True,            # la consola ES la interfaz: muestra la URL y el paso del certificado
    disable_windowed_traceback=False,
    icon=str(ROOT / "packaging" / "agrosuite.ico")
        if (ROOT / "packaging" / "agrosuite.ico").exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="AgroSuite",
)

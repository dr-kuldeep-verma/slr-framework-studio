# PyInstaller one-folder build for Windows.
#
#   pip install -e .[gui,dev]
#   pyinstaller packaging/slr_studio.spec --noconfirm
#
# The result is dist/SLRFrameworkStudio/, which packaging/installer.iss turns
# into a single setup.exe. One-folder rather than one-file: a one-file build
# unpacks to a temporary directory on every launch, which makes startup slow
# and trips corporate antivirus on exactly the machines researchers use.

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

project_root = Path(SPECPATH).parent
src = project_root / "src"

# Everything the demonstration project reads. It is the first thing a new
# user presses, and in the first Windows build it stopped halfway through
# because the PDFs were on disk but not in the bundle. A test compares this
# list against what the package actually has.
datas = [
    (str(src / "slr_studio" / "demo" / "data"), "slr_studio/demo/data"),
    (str(src / "slr_studio" / "demo" / "pdfs"), "slr_studio/demo/pdfs"),
    # Handing somebody a copy of this program is conveying an AGPL work,
    # and the licence has to travel with it. Without these two files the
    # folder a reviewer downloads states no terms at all -- and the Qt
    # libraries sitting beside them are LGPL, which has notice obligations
    # of its own.
    (str(project_root / "LICENSE"), "."),
    (str(project_root / "THIRD-PARTY-NOTICES.md"), "."),
]

_resources = src / "slr_studio" / "resources"
if _resources.is_dir():
    datas.append((str(_resources), "slr_studio/resources"))

# Tesseract is bundled so OCR works without the reviewer installing anything.
# Drop a Windows Tesseract build into packaging/vendor/tesseract before
# building; the full-text stage looks for it beside the executable.
tesseract_dir = project_root / "packaging" / "vendor" / "tesseract"
if tesseract_dir.is_dir():
    datas.append((str(tesseract_dir), "vendor/tesseract"))

fonts_dir = project_root / "packaging" / "vendor" / "fonts"
if fonts_dir.is_dir():
    datas.append((str(fonts_dir), "vendor/fonts"))

icon_path = project_root / "packaging" / "app.ico"

a = Analysis(
    [str(src / "slr_studio" / "app.py")],
    pathex=[str(src)],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "slr_studio.ui.main_window",
        "slr_studio.ui.pages",
        "slr_studio.importers.registry",
        "slr_studio.dedup.engine",
        "slr_studio.synthesis.engine",
        "slr_studio.framework.engine",
        "slr_studio.gaps.engine",
        "slr_studio.exports.bundle",
        "reportlab.pdfgen",
        "reportlab.lib.pagesizes",
        "docx",
        # The figure writers are reached through functions, not imports
        # PyInstaller can see.
        "pptx",
        "PIL.Image",
        "PIL.ImageDraw",
        "PIL.ImageFont",
        # pkg_resources' runtime hook imports this and PyInstaller does not
        # follow it. Without it the built application fails on startup with
        # "The 'platformdirs' package is required", which is what a reviewer
        # sees instead of the window.
        "platformdirs",
        "jaraco.text",
        # Sign in with ChatGPT: the ID-token check loads its RSA support
        # lazily, out of PyInstaller's sight.
        "jwt.algorithms",
        "cryptography.hazmat.primitives.asymmetric.rsa",
        "cryptography.hazmat.primitives.asymmetric.ec",
    ],
    hookspath=[],
    runtime_hooks=[],
    # Qt ships far more than this application uses; excluding the unused
    # modules keeps the installer near 90 MB instead of several hundred.
    excludes=[
        "PySide6.Qt3DCore",
        "PySide6.QtQuick",
        "PySide6.QtQml",
        "PySide6.QtWebEngineCore",
        "PySide6.QtMultimedia",
        "tkinter",
        "matplotlib",
        # None of these is imported anywhere in this application. They are
        # excluded by name because PyInstaller bundles whatever it finds
        # installed alongside, and a machine that happens to have scipy and
        # OpenCV on it produced a 694 MB build of a program that uses
        # neither. Naming them keeps the build the same size wherever it is
        # made.
        "numpy",
        "scipy",
        "pandas",
        "cv2",
        "onnxruntime",
        "sklearn",
        "torch",
        "IPython",
        "notebook",
        "pytest",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

# Two executables from the one analysis, because Windows gives a windowed
# program no console at all. Built with console=False alone, everything the
# pipeline prints -- progress, costs, the reason a run stopped, even
# --version -- goes nowhere: the build script's own check of the finished
# executable printed a blank line and reported success.
#
#   SLRFrameworkStudio.exe   the window. No console flashing up behind it.
#   slr.exe                  the same program with a console, for the
#                            command line, for scripting, and for checking
#                            a build actually works.
exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="SLRFrameworkStudio",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(icon_path) if icon_path.is_file() else None,
    version=str(project_root / "packaging" / "version_info.txt"),
)

console_exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="slr",
    debug=False,
    strip=False,
    upx=False,
    console=True,
    icon=str(icon_path) if icon_path.is_file() else None,
    version=str(project_root / "packaging" / "version_info.txt"),
)

coll = COLLECT(
    exe,
    console_exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="SLRFrameworkStudio",
)

# Third-party notices

SLR Framework Studio is licensed under the GNU Affero General Public
License version 3 or later (see `LICENSE`). It is built on the libraries
below, each under its own licence. A packaged Windows build contains
copies of all of them.

Licences here are read from the packages actually installed. If you add a
dependency, add it here: a notices file that quietly omits something is
worse than none, because it reads as a complete list.

## Why this project is AGPL

This is not a free choice. **PyMuPDF**, which reads every PDF the review
stage opens, is licensed by Artifex under the AGPL-3.0 or a paid
commercial licence. A program that bundles AGPL code and is distributed to
anyone else must itself be AGPL. The alternative was to drop PyMuPDF for a
permissively licensed PDF library and release under a permissive licence;
that was considered and not taken, because the quotation-verification this
application depends on — checking that every quoted sentence really is on
the page it cites — is only as good as the text extraction underneath it.

### What that means for you

- **Using it for your own research**: no obligations whatsoever. Install
  it, run it, review with it, publish your findings. The AGPL places
  requirements on people who *distribute* the software or run a modified
  version as a *network service*, not on people who use it.
- **Modifying it for yourself**: also no obligations, as long as you keep
  the modified version to yourself.
- **Distributing it, modified or not**: you must pass on the source,
  under the AGPL, including your modifications.
- **Running a modified version as a public network service**: section 13
  of the AGPL requires you to offer its users the source of the version
  you are running. This application is a desktop program and does not do
  that, but the clause binds anyone who makes it into a web service.

## Dependencies

| Library | Version tested | Licence | What it does here |
|---|---|---|---|
| [PyMuPDF](https://pymupdf.readthedocs.io/) | 2.0 | **AGPL-3.0** or Artifex commercial | Reads PDFs: text, page geometry, and rasterising pages for OCR |
| [PySide6](https://www.qt.io/qt-for-python) (Qt) | 6.12.0 | **LGPL-3.0** or GPL-2.0/3.0 or commercial | The desktop interface |
| [pydantic](https://docs.pydantic.dev/) | 2.13.5 | MIT | Validates every JSON reply a model returns |
| [openpyxl](https://openpyxl.readthedocs.io/) | 3.1.5 | MIT | The Excel screening log and extraction matrix |
| [python-docx](https://python-docx.readthedocs.io/) | 1.2.0 | MIT | Word exports: PRISMA diagram, checklist, Methods draft |
| [python-pptx](https://python-pptx.readthedocs.io/) | 1.0.2 | MIT | The editable PowerPoint figures |
| [keyring](https://github.com/jaraco/keyring) | 25.7.0 | MIT | Stores API keys in the Windows Credential Manager |
| [Pillow](https://python-pillow.org/) | 12.3.0 | MIT-CMU (HPND) | Draws the PNG figures and the evidence gap map |
| [reportlab](https://www.reportlab.com/) | 5.0.1 | BSD | The PDF exports |
| [httpx](https://www.python-httpx.org/) | 0.28.1 | BSD-3-Clause | Calls to the model provider and to Unpaywall |
| [python-dateutil](https://github.com/dateutil/dateutil) | 2.9.0 | Apache-2.0 or BSD-3-Clause (dual) | Parsing dates out of database exports |
| [pytesseract](https://github.com/madmaze/pytesseract) | 0.3.13 | Apache-2.0 | Optional: OCR for scanned PDFs |

Tesseract itself, if you install it for OCR, is Apache-2.0 and is not
bundled with this application.

## Qt / PySide6 under the LGPL

PySide6 and the Qt libraries beneath it are used under the **LGPL version
3**, not the GPL and not a commercial licence. The LGPL allows that on
conditions this build is arranged to meet:

- Qt is **dynamically linked**. The packaged application is a one-folder
  PyInstaller build in which the Qt libraries are separate files
  (`PySide6/*.dll` and friends) beside the executable, not statically
  linked into it.
- You may therefore **replace them**. Substituting a compatible build of
  Qt into that folder is enough to run the application against your own
  Qt, which is the freedom the LGPL exists to protect.
- The Qt source is available from <https://download.qt.io/> and the
  PySide6 source from <https://pypi.org/project/PySide6/>. Neither is
  modified here.

A one-folder build was chosen originally because a one-file build unpacks
to a temporary directory on every launch, which is slow and trips
corporate antivirus on exactly the machines researchers use. That it also
satisfies the LGPL is a convenience, not the reason.

## Data and fixtures

The demonstration project under `src/slr_studio/demo/` — its records, its
PDFs and its decisions — is generated for this repository and is part of
the work, under the same licence. It describes no real study, and nothing
in it is a finding about anything.

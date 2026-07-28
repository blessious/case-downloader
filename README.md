# Case PDF Downloader

A local, cross-platform web application that turns assigned Philippine case lists into organized full-text PDFs. It checks Lawphil first, then the Supreme Court E-Library and ChanRobles as fallbacks, validates every PDF, and records every case that was not saved.

## Features

- Paste a numbered case list or upload a `.docx` file.
- Review and correct parsed titles, G.R. numbers, dates, and week headings.
- Choose an absolute destination path on Windows or macOS.
- Save title-only filenames inside `Week N` folders.
- Preserve existing files; never overwrite silently.
- See live status and retry unsuccessful cases.
- Keep local run history in SQLite.
- Export a formatted Excel report with `Summary`, `All Cases`, `Not Saved`, and `Attempts` sheets.

## Windows setup

Open PowerShell in this project and run:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup-windows.ps1
.\scripts\start-windows.bat
```

## macOS setup

Open Terminal in this project and run:

```bash
chmod +x scripts/*.sh scripts/*.command
./scripts/setup-macos.sh
./scripts/start-macos.command
```

The application opens at a local `127.0.0.1` address. It is not exposed to other computers.

## Packaging

Packages must be built on their target operating system because Chromium and executable formats are platform-specific.

- Windows: `powershell -ExecutionPolicy Bypass -File .\scripts\build-windows.ps1`
- macOS: `./scripts/build-macos.sh`

The packaged Chromium browser makes the distribution large, but it lets HTML decisions render to PDF without relying on the user’s default browser.

## Filename rules

The assigned title is preserved, while `v`, `vs`, and `vs.` are normalized to `v.`. Only filesystem-invalid characters are replaced. G.R. numbers and dates are never appended.

Example:

```text
Honda Philippines, Inc. v. Samahan ng Malalaya sa Honda.pdf
```

## Privacy and website access

Run history stays in the current user’s application-data directory. Only Lawphil, the Supreme Court E-Library, and ChanRobles are contacted. Requests are rate-limited, and the application does not bypass CAPTCHA, login, or access controls.

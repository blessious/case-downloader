# Case PDF Downloader

A local, cross-platform web application that turns assigned Philippine case lists into organized full-text PDFs. It searches official indexes first, broadens missed cases through Google in the packaged browser, downloads only from approved legal sources, and verifies every PDF.

## Features

- Paste a case list or upload a `.docx` or text-based `.pdf` syllabus.
- Select specific PDF pages and choose the folder name for each uploaded syllabus.
- Recognize numbered, bulleted, and unnumbered cases, including day-first dates and SCRA/Phil citations.
- Search progressively by G.R./L-number, SCRA/Phil citation, party names, date, and the original syllabus entry.
- Use Google only for cases unresolved by official-source lookup, with at most three browser searches per case.
- Present uncertain verified matches as compact choices instead of prematurely marking them not found.
- Review the original syllabus entries in a compact list before starting.
- Choose an absolute destination path on Windows or macOS.
- Save title-only filenames inside week, module, or section folders.
- Preserve existing files; never overwrite silently.
- See live status and retry unsuccessful cases.
- Process up to four case downloads concurrently by default.
- Keep run data locally in SQLite.

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

The macOS build produces one distributable file in `dist`, named for the Mac architecture:

```text
Case PDF Downloader-macOS-Apple-Silicon.dmg
Case PDF Downloader-macOS-Intel.dmg
```

Send the appropriate `.dmg` to the Mac user. They open it, drag **Case PDF Downloader** to the Applications shortcut, and launch it from Applications. An unsigned build may require right-clicking the app and selecting **Open** on first launch.

For a signed build, export the Developer ID certificate name before running the script:

```bash
export APPLE_SIGNING_IDENTITY="Developer ID Application: Your Name (TEAMID)"
./scripts/build-macos.sh
```

For notarization, first save App Store Connect credentials in the macOS keychain with `xcrun notarytool store-credentials`, then also export its profile name:

```bash
export APPLE_SIGNING_IDENTITY="Developer ID Application: Your Name (TEAMID)"
export APPLE_NOTARY_PROFILE="case-pdf-downloader"
./scripts/build-macos.sh
```

The packaged Chromium browser makes the distribution large, but it lets HTML decisions render to PDF without relying on the user’s default browser.

### Build macOS package from Windows

The GitHub Actions workflow can build the Mac package without access to a Mac:

1. Push the project to GitHub.
2. Open the repository’s **Actions** tab.
3. Select **Build Mac DMG**.
4. Choose **Run workflow**, then confirm **Run workflow**.
5. When the run finishes, download **Case-PDF-Downloader-macOS** from its Artifacts section.

The downloaded DMG is the only file that needs to be sent to the Mac user.

## Filename rules

The assigned title is preserved, while `v`, `vs`, `vs.`, and `versus` are normalized to `v.`. Only filesystem-invalid characters are replaced. G.R. numbers and dates are never appended.

Example:

```text
Honda Philippines, Inc. v. Samahan ng Malalaya sa Honda.pdf
```

## Privacy and website access

Run history stays in the current user’s application-data directory. Google receives search queries only for unresolved cases. Final documents are accepted only from Lawphil, the Supreme Court E-Library, the Supreme Court website, and ChanRobles. Other results may supply clues but are never saved. Google searches are serialized and CAPTCHA is never bypassed; when Google blocks automation, the app offers the same search for the user to open manually.

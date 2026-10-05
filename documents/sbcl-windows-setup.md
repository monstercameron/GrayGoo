# SBCL on Windows (user-local, no admin)

Status (2026-10-05): **SBCL 2.6.9 verified working** on this machine
(ARM64 Windows) with zero admin rights and zero system-path changes.
The `graygoo` ASDF system loads cleanly under it (25/25 packages).

## Install (user-local)

The winget package `SBCL.SBCL` ships a machine-scope MSI, so a plain
`winget install` needs elevation and `--scope user` fails with
"No applicable installer found". Instead, download via winget
(hash-verified) and extract with `msiexec /a` (administrative install =
file extraction, no registry/system changes, no admin needed):

```powershell
$dest = Join-Path $env:LOCALAPPDATA 'sbcl-local'
New-Item -ItemType Directory -Force -Path $dest | Out-Null
winget download --id SBCL.SBCL --download-directory $dest --accept-source-agreements
$msi = Get-ChildItem $dest -Filter '*.msi' | Select-Object -First 1 -ExpandProperty FullName
$target = Join-Path $dest 'sbcl-2.6.9'
Start-Process msiexec.exe -ArgumentList "/a `"$msi`" TARGETDIR=`"$target`" /qn" -Wait
```

Resulting binary on this machine:

```text
%LOCALAPPDATA%\sbcl-local\sbcl-2.6.9\PFiles\Steel Bank Common Lisp\sbcl.exe
```

Do NOT download the MSI straight from `prdownloads.sourceforge.net` with
`Invoke-WebRequest`: SourceForge answers with a mirror-selection HTML page.
`winget download` follows the mirror chain and verifies the SHA256.

## Put it on PATH (current user only, optional)

Session-only:

```powershell
$env:PATH = 'C:\Users\mreca\AppData\Local\sbcl-local\sbcl-2.6.9\PFiles\Steel Bank Common Lisp;' + $env:PATH
```

Persistent (user, not system): `setx PATH` with the same prefix, or add it
via Settings → Environment variables → User variables.

## Verify

```powershell
& "$env:LOCALAPPDATA\sbcl-local\sbcl-2.6.9\PFiles\Steel Bank Common Lisp\sbcl.exe" --version
# SBCL 2.6.9
```

Non-interactive load of the `graygoo` system from the repo root:

```powershell
$sbcl = "$env:LOCALAPPDATA\sbcl-local\sbcl-2.6.9\PFiles\Steel Bank Common Lisp\sbcl.exe"
& $sbcl --non-interactive --eval '(require :asdf)' `
  --eval '(push *default-pathname-defaults* asdf:*central-registry*)' `
  --eval '(asdf:load-system :graygoo)' `
  --eval '(format t "~&graygoo ~A loaded.~%" (evo.kernel:runtime-version))'
```

Verification evidence on this machine: `SBCL 2.6.9 | ASDF 3.3.1`,
`PACKAGES OK: 25/25`, dispatch/events/registry/metrics/transfer smoke
tests pass, unimplemented-provider stubs correctly signal errors, exit 0.

## Interactive development (SLY/Swank)

Not yet installed on this machine; the user-local recipe is:

1. Install Quicklisp to `%USERPROFILE%\quicklisp` (user-local, no admin):
   download `quicklisp.lisp`, then
   `sbcl --load quicklisp.lisp --eval '(quicklisp-quickstart:install)'`.
2. `(ql:quickload "slynk")` once, to fetch the SLY backend.
3. In Emacs, install the `sly` package (MELPA) and set
   `inferior-lisp-program` to the `sbcl.exe` path above.

## Troubleshooting

- **PowerShell eats double quotes** when passing `--eval` strings to
  `sbcl.exe` (a native command). If an eval needs string literals, write
  the Lisp program to a `.lisp` file with `Set-Content` and pass it via
  `--load file.lisp` instead.
- **Never plain-`LOAD` a `.asd` file**: `DEFSYSTEM` is not visible in
  `CL-USER`, so `(load "graygoo.asd")` fails. Use
  `(asdf:load-asd ...)` or push the repo root onto
  `asdf:*central-registry*` and call `(asdf:load-system :graygoo)`.
- ASDF writes fasls to `%LOCALAPPDATA%\cache\common-lisp\...`, keeping
  the checkout clean.

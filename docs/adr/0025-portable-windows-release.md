# ADR 0025: Portable Windows release

Status: Accepted for v1.0.0.

## Context

The source checkout requires Python 3.11 and uv. Competition reviewers and other users need a
downloadable build that starts the Chinese workbench without installing a development toolchain.

## Decision

Publish a Windows x64 one-directory PyInstaller build as a ZIP attached to each GitHub Release.
`SciDataFusion.exe` binds only to `127.0.0.1`, selects an available port from 8000 through 8099,
starts FastAPI through Uvicorn, and opens the default browser after the health endpoint responds.

The release includes package data, Astropy, Polars, versioned prompts, `.env.example`, a batch
launcher, and a Chinese usage guide. It deliberately excludes `.env` and all real credentials.
The writable runtime directory is the extracted folder, so browser configuration remains local.

Git tags matching `v*` trigger the Windows build and publish the ZIP plus its SHA-256 checksum.

## Consequences

- Users can extract the ZIP and run the application without Python, uv, or Git.
- The package is Windows x64 specific and larger than the source distribution.
- Every release must pass source gates and a clean-directory executable smoke test.

# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for cothink-serve.
#
# Builds a single executable that wraps the cothink FastAPI HTTP+SSE bridge
# (src/cothink/server.py:main) and bundles all Python deps. The cothink fork's
# Electron app spawns this as a child process on startup and kills it on
# before-quit.
#
# USAGE (per platform):
#   pip install pyinstaller==6.10.0
#   pyinstaller cothink-serve.spec --clean --noconfirm
#   # output lands in dist/cothink-serve(.exe) — single file, no dependencies
#
# PyInstaller produces a binary for the HOST PLATFORM ONLY. Cross-platform
# builds require running pyinstaller on each target OS — typically in the
# fork's GitHub Actions matrix (Windows, macOS, Linux).
#
# Maintenance note: when adding a new top-level dependency to pyproject.toml,
# audit whether it needs to appear in hiddenimports below. Run-time
# `ModuleNotFoundError` from the bundled binary almost always means a missing
# hiddenimport.

from __future__ import annotations

import sys
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_submodules,
    copy_metadata,
)

block_cipher = None

# ---------------------------------------------------------------------------
# Project layout
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(SPECPATH).resolve()
SRC = PROJECT_ROOT / "src"

# Tiny launcher script — PyInstaller needs a concrete file as the entry point.
# We synthesize one inline so we don't pollute the package with a build-only
# stub. (The console-script entry `cothink-serve = cothink.server:main` lives
# in pyproject.toml and is intentionally NOT used here because PyInstaller
# can't read setuptools console_scripts.)
ENTRY = PROJECT_ROOT / "_pyinstaller_entry.py"
ENTRY.write_text(
    "from cothink.server import main\n"
    "if __name__ == '__main__':\n"
    "    main()\n",
    encoding="utf-8",
)

# ---------------------------------------------------------------------------
# Hidden imports — libraries with dynamic dispatch that PyInstaller's static
# analyzer misses. Empirical list; expand as ModuleNotFoundError surfaces at
# run-time during smoke tests.
# ---------------------------------------------------------------------------

# LangGraph: uses string-keyed node/edge dispatch internally; its submodules
# (checkpoint, prebuilt, pregel) are not statically reachable from `import
# langgraph`. Use collect_submodules to grab everything under langgraph.*
# without listing each one by hand.
hiddenimports = list(collect_submodules("langgraph"))

# claude-agent-sdk: newer SDK, less battle-tested with PyInstaller. Pull all
# submodules to be safe (it's small).
hiddenimports += collect_submodules("claude_agent_sdk")

# google-genai: lots of protobuf-generated code + dynamic resource loaders for
# Google's HTTP transport. Collect everything; google.api_core in particular
# has been a known PyInstaller footgun.
hiddenimports += collect_submodules("google.genai")
hiddenimports += [
    "google.api_core",
    "google.auth",
    "google.auth.transport.requests",
]

# uvicorn[standard] pulls in optional deps that PyInstaller's hooks usually
# catch, but list them explicitly so the bundled binary doesn't error out
# when uvicorn auto-selects an http parser or websocket impl at startup.
hiddenimports += [
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.protocols.websockets.wsproto_impl",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.logging",
    "httptools",
    "websockets",
    "wsproto",
    "h11",
]

# uvloop is Unix-only; including it on Windows would error. Branch on platform.
if sys.platform != "win32":
    hiddenimports.append("uvloop")
    hiddenimports.append("uvicorn.loops.uvloop")

# sse-starlette: small library, but its EventSourceResponse uses anyio under
# the hood — make anyio's backends explicit.
hiddenimports += [
    "sse_starlette",
    "anyio",
    "anyio._backends._asyncio",
]

# pydantic v2 + instructor: pydantic_core is a Rust shared lib that PyInstaller
# usually catches via its bundled hook, but instructor's response-model dispatch
# uses dynamic imports we list explicitly.
hiddenimports += [
    "pydantic",
    "pydantic_core",
    "pydantic.deprecated.decorator",
    "instructor",
    "instructor.dsl",
]

# filelock: trivial, but its platform-specific lock impls are picked at import
# time via sys.platform — list both to be safe.
hiddenimports += [
    "filelock._windows",
    "filelock._unix",
]

# Our own package — explicitly list every submodule so PyInstaller bundles
# them all, even ones imported lazily via the FastAPI route handlers.
hiddenimports += [
    "cothink.chat",
    "cothink.cli",
    "cothink.edges",
    "cothink.graph",
    "cothink.image_handler",
    "cothink.learnings_enforcer",
    "cothink.memory",
    "cothink.nodes",
    "cothink.project_state",
    "cothink.prompts",
    "cothink.server",
    "cothink.sessions",
    "cothink.state",
]

# ---------------------------------------------------------------------------
# Data files — packages that ship JSON/YAML/template files alongside their
# Python code. PyInstaller needs to know to bundle these data files too.
# ---------------------------------------------------------------------------

datas = []

# pydantic-core ships a compiled extension + JSON schema; collect_data_files
# walks the installed package to pick up everything.
datas += collect_data_files("pydantic")

# google-genai ships proto descriptors as data files.
datas += collect_data_files("google.genai")

# claude-agent-sdk ships system prompts as package data.
datas += collect_data_files("claude_agent_sdk")

# Package metadata (importlib.metadata.version("...") lookups happen at
# runtime in several libraries; copy_metadata makes them work in the bundle).
for pkg in (
    "fastapi",
    "starlette",
    "uvicorn",
    "pydantic",
    "langgraph",
    "claude-agent-sdk",
    "google-genai",
    "sse-starlette",
):
    try:
        datas += copy_metadata(pkg)
    except Exception:
        # If a pkg isn't installed in the build env, skip it rather than
        # failing the whole spec.
        pass

# ---------------------------------------------------------------------------
# Analysis stage
# ---------------------------------------------------------------------------

a = Analysis(
    [str(ENTRY)],
    pathex=[str(SRC)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Large libraries we don't use — exclude to shrink the binary.
        "tkinter",
        "matplotlib",
        "PIL.ImageTk",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
        "IPython",
        "notebook",
        "jupyter",
        "pandas",
        "numpy.distutils",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# ---------------------------------------------------------------------------
# Build the executable (--onefile equivalent)
# ---------------------------------------------------------------------------

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="cothink-serve",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # No icon for v0.1: PyInstaller wants .ico (Windows) / .icns (macOS), and
    # we currently only ship the dual-hemisphere mark as SVG. Generate
    # platform-native icons + wire them in at v0.5+ when the cothink fork is
    # also embedding the binary into the Electron payload.
    icon=None,
)

# Remember Profile Project

## Goal

After `tokenmaxxing profile init DIRECTORY` succeeds, profile commands should
work from any directory without requiring the user to `cd` into the project or
repeat `--config`.

## Design

Tokenmaxxing stores the absolute path to the initialized `config.yaml` in
its existing platform-specific application-data directory. The pointer is
local machine state, separate from the editable profile project and its YAML.

Profile configuration resolves in this order:

1. An explicit `profile --config PATH` argument.
2. `config.yaml` in the current directory or one of its parents.
3. The remembered profile path.

This keeps commands predictable inside any profile project while making the
most recently initialized project available elsewhere. Initialization updates
the pointer only after the project and configuration have been created and
validated successfully.

## Failure handling

An absent pointer preserves the existing `could not find config.yaml`
error. If a remembered path no longer names a file, the error identifies that
path and tells the user to initialize a profile again or pass `--config`.
Malformed pointer state is treated the same way and never changes profile YAML.

## Portability and storage

The pointer uses the application-data directory already selected by
`default_paths`: Application Support on macOS, XDG data on Linux, and Local
AppData on Windows. Its parent directory is created as needed. The stored file
contains one UTF-8 absolute path and is replaced atomically.

## Tests

Tests cover resolution precedence, remembering a successful initialization,
using the remembered profile outside its project, missing and stale pointer
behavior, and platform-independent application-data paths. The existing
profile CLI and package workflow tests remain green.

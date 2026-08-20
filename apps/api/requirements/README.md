# Python dependencies, and how to change one

`requirements.lock` is what the API image installs, under `--require-hashes`.
The `.txt` files remain the human-edited inputs; the lock is generated from
them and is never hand-edited.

## Why a hash lock at all

Not for the reason usually given, and the distinction is the whole point:

* All **43** direct dependencies (42 in `base.txt` plus `gunicorn` in
  `production.txt`) were **already** pinned exactly. **PyPI does not permit a
  filename to be re-used** — not for a re-upload, and not after a deletion — so
  an exact pin already names specific bytes *for artifacts that exist*. That is
  narrower than "a version is a digest": a pin says nothing about an artifact
  the index has never served, and nothing about who served it to you. What it
  does defeat is retroactive substitution of a published file, which is the
  attack a hash on an exact-pinned direct dependency would be defending against.
  **The Docker argument does not transfer**, because a Docker tag genuinely is a
  mutable pointer to whatever was pushed last.
* What the lock actually buys is **completeness**. `--require-hashes` refuses
  to install anything unpinned, which forces the transitive set to be
  enumerated: 43 direct dependencies became **114 pinned packages**. Before
  this, a rebuild months apart could produce different transitive versions
  with no record of what changed.
* It also removes an undeclared assumption. Hashes give real tamper resistance
  against an untrusted index — a caching proxy, an internal mirror, a second
  index enabling dependency confusion. We have none today, and nothing
  enforces that we never will.

## Changing a dependency

Edit the `.txt` file, then regenerate **inside a container that has the build
toolchain**. This is not optional and it is not a convenience:

Run everything below from `apps/api` — the paths are relative to it.

**One command does the whole thing**, because generation and stamping used to be
two steps and the documented recipe silently dropped the stamp — following it
produced a lock that failed the build (Morrow RC 3678):

```sh
cd apps/api

docker build -t biplane-lockgen -f - . <<'EOF'
FROM python:3.12.10-alpine@sha256:4bbf5ef9ce4b273299d394de268ad6018e10a9375d7efc7c2ce9501a6eb6b86c
RUN apk add --no-cache bash g++ gcc cargo git make postgresql-dev libc-dev linux-headers
RUN pip install --no-cache-dir pip-tools
WORKDIR /w
EOF

docker run --rm -v "$PWD:/w" -w /w biplane-lockgen \
  python requirements/lock.py generate
```

`requirements/lock.py` is the single owner of three things that used to be
separate and disagreed at the seams: **which files are the declared inputs**,
**how the lock is generated**, and **how the build verifies it**. Generation and
verification read the same input walk, and generation always stamps.

**Why the toolchain must be present**: `pip-compile` builds metadata for
packages with no musl wheel, and `psycopg-c` needs `pg_config`. Running this in
the runtime image fails with `[Errno 2] No such file or directory: 'pg_config'`
— the Dockerfile installs those build deps and then deletes them.

**Why in a container at all**: the lock is resolved against a specific Python
version and platform. Generating it on a different interpreter records
different resolutions; markers are evaluated at compile time.

## Verifying a change

Building the image IS the verification, and it must be done — a hash set that
misses an artifact the build fetches fails only at build time, with a confusing
error, at the worst possible moment:

```sh
docker build -f Dockerfile.api .
```

To confirm the enforcement is live rather than assumed, corrupt a hash and
watch it fail. Note that pip accepts a package if **any** listed hash matches,
so corrupting one of a package's two hashes proves nothing — change them all:

```
ERROR: THESE PACKAGES DO NOT MATCH THE HASHES FROM THE REQUIREMENTS FILE.
        Expected sha256 0000000000000000000000000000000000000000000000000000000000000000
             Got        43b3319e1b4e7d1251833a93d672b4af1e40f3d632d479b98661a95f117880a2
```

## Freshness: the lock must describe the current inputs

`requirements.lock` carries an `# inputs-sha256:` stamp over the input files it
was generated from, and `requirements/lock.py check` runs in the image
build **before any package is fetched**. Editing an input without regenerating
fails the build:

```
lock freshness: requirements.lock does not describe the current inputs.
  stamped: 09bdd976b605...
  inputs:  b0c60db1d691...
```

Without it the two halves were disconnected: `--require-hashes` makes the
install honest about its *contents*, and nothing made the lock honest about its
*inputs* — so editing a `.txt` and rebuilding produced a **successful** image
installing the old dependency graph (Morrow RC 3671).

The stamp covers `requirements.txt` and what it includes, and hashes each
file's NAME as well as its bytes, so moving a requirement between two input
files does not leave the digest unchanged. Regenerating the lock regenerates
the stamp.

## Scope: what the lock and the gate actually cover

Confirmed by running the input walk rather than by reading the files:

```
requirements.txt  ->  production.txt  ->  base.txt
```

**That is the whole input set.** `test.txt` and `local.txt` are not inputs, so
**neither the hash requirement nor the freshness stamp covers anything a test
run installs**. CI installs `test.txt`, and a compromised index still reaches
the test runner after this lands. Stated as a scoped exclusion rather than left
to be inferred (Aria, Sable).

## What this does NOT cover

* **`apk` packages.** `bash`, `gcc`, `postgresql-dev` and the rest come from
  Alpine's package repository, which is mutable. No Python lock touches them.
* **`requirements/test.txt` and `local.txt` are not locked, and this is a known
  accepted gap rather than an oversight** (Aria). The lock is generated from
  `requirements.txt`, which is production only, so nothing here protects the
  *test runner*: CI installs `test.txt` unpinned-transitively, and a compromised
  index still reaches it. `local.txt` is deliberately excluded so a formatter
  bump cannot force a production-lock regeneration. Locking the test set is a
  separate, defensible piece of work; it is simply not this one.

* **Environment markers.** This lock carries none, because it is resolved for
  the one platform the image is built on — Alpine, CPython 3.12 — and
  conditional dependencies for other platforms (`colorama; platform_system ==
  "Windows"`, `tzdata; sys_platform == "win32"`) are correctly resolved out.
  **Do not reuse this lock to build for another platform**; regenerate it there.
  Architecture is a different axis and IS covered: `pip-compile` records the
  hash of *every* artifact of each version, verified — `cryptography` carries
  49 hashes against exactly 49 artifacts on PyPI, including 6 musllinux and 31
  manylinux wheels, so a build on either architecture finds a match. That is
  the difference from a Docker digest, which names one image and needs the
  manifest list to span architectures.

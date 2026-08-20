"""A parity test must EXECUTE BOTH implementations and compare them.

The previous version asserted only the Python side against the corpus, so
replacing the shell validator with `return 0` left it green — it was a Python
conformance test wearing a parity test's name (Rowan, RC 3493). That is the
measuring-instrument failure, in the artifact built to prevent divergence.

Every case below runs the SHELL implementation through its CLI and the PYTHON
implementation in-process, and fails if they disagree — regardless of which one
is right. A separate set of cases pins the shared corpus's expected verdicts, so
"both agree" cannot pass by both being wrong together.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from plane.license.utils import release_version

_HERE = Path(release_version.__file__).resolve().parent
_SHELL = _HERE / "release_version.sh"
_CORPUS = _HERE / "release_version_corpus.tsv"


def _shell(*argv: str) -> bool:
    """This implementation's verdict, obtained by running it — not modelled."""
    result = subprocess.run(
        ["bash", str(_SHELL), *argv], capture_output=True, text=True
    )
    if result.returncode not in (0, 1, 2):
        raise AssertionError(
            "shell authority exited %d: %s" % (result.returncode, result.stderr)
        )
    return result.returncode == 0


def _cases():
    for line in _CORPUS.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        yield parts[0], (parts[1] if len(parts) > 1 else ""), (parts[2] if len(parts) > 2 else "")


CASES = list(_cases())


def test_the_shell_authority_is_actually_reachable():
    """Without this, every parity case below could pass by never running it."""
    assert _SHELL.is_file(), _SHELL
    assert _shell("valid", "v1.2.3") is True
    assert _shell("valid", "v01.2.3") is False


@pytest.mark.parametrize("verdict,a,b", CASES)
def test_both_implementations_agree(verdict, a, b):
    if verdict in ("accept", "refuse"):
        assert _shell("valid", a) == release_version.is_valid(a), (
            "shell and python disagree on validity of %r" % (a,)
        )
    else:
        assert _shell("gt", a, b) == release_version.gt(a, b), (
            "shell and python disagree on %r > %r" % (a, b)
        )


@pytest.mark.parametrize("verdict,a,b", CASES)
def test_the_corpus_verdicts_hold(verdict, a, b):
    """Agreement is not enough — both could be wrong together."""
    if verdict == "accept":
        assert release_version.is_valid(a)
    elif verdict == "refuse":
        assert not release_version.is_valid(a)
    elif verdict == "gt":
        assert release_version.gt(a, b)
    elif verdict == "ngt":
        assert not release_version.gt(a, b)
    else:
        pytest.fail("unknown verdict %r" % verdict)


def test_unicode_digits_are_refused():
    assert not release_version.is_valid("v١.٢.٣")
    assert _shell("valid", "v١.٢.٣") is False


def test_trailing_newline_is_refused():
    assert not release_version.is_valid("v1.2.3\n")


def test_derived_edges_are_computed_not_stored():
    assert release_version.is_valid(release_version.upper_accepted())
    assert not release_version.is_valid(release_version.first_refused())
    assert "upper" not in release_version.DATUM_PATH.read_text(encoding="utf-8").lower()


_DATUM_MATRIX = _HERE / "release_version_datum_matrix.tsv"


def _datum_cases():
    for line in _DATUM_MATRIX.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        yield parts[0], (parts[1] if len(parts) > 1 else "")


@pytest.mark.parametrize("verdict,raw", list(_datum_cases()))
def test_datum_reader_matrix(verdict, raw, tmp_path):
    """The corpus varies version VALUES; nothing varied the AUTHORITY DATUM.

    That is exactly why two readers drifted on it undetected — this side
    stripped whitespace and int()-normalised while the shell matched raw text
    (Morrow, RC 3492/3496). Both adapters now run these identical bytes.
    """
    written = tmp_path / "datum"
    written.write_bytes(raw.encode("utf-8").decode("unicode_escape").encode("utf-8"))
    if verdict == "accept":
        assert release_version._read_max_component_digits(written) == int(
            written.read_text(encoding="utf-8").strip()
        )
    else:
        with pytest.raises(Exception):
            release_version._read_max_component_digits(written)


@pytest.mark.parametrize("verdict,raw", list(_datum_cases()))
def test_both_datum_readers_agree(verdict, raw, tmp_path):
    """Agreement on the datum, not just on versions — the drift was here."""
    written = tmp_path / "datum"
    written.write_bytes(raw.encode("utf-8").decode("unicode_escape").encode("utf-8"))

    try:
        release_version._read_max_component_digits(written)
        python_ok = True
    except Exception:
        python_ok = False

    shell = subprocess.run(
        ["bash", "-c", '. "$1"; _rv_read_datum "$2"', "_", str(_SHELL), str(written)],
        capture_output=True,
        text=True,
    )
    assert python_ok == (shell.returncode == 0), (
        "datum readers disagree on %r: python=%s shell=%s"
        % (raw, python_ok, shell.returncode == 0)
    )

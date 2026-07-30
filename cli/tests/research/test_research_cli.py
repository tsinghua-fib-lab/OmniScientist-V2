"""CLI smoke tests for the ROM groups (omni hypo/claim/evidence/run/source)."""

from __future__ import annotations

import re

from typer.testing import CliRunner

from omni.cli.main import app

runner = CliRunner()
_HEX8 = re.compile(r"[0-9a-f]{8}")


def _first_id(output: str) -> str:
    m = _HEX8.search(output)
    assert m, f"no 8-hex id found in: {output!r}"
    return m.group(0)


def test_hypo_lifecycle():
    new = runner.invoke(app, ["hypo", "new", "Sparse attention scales better",
                              "--confidence", "0.4"])
    assert new.exit_code == 0, new.stdout
    hid = _first_id(new.stdout)

    listed = runner.invoke(app, ["hypo", "list"])
    assert listed.exit_code == 0
    assert "Sparse attention" in listed.stdout

    upd = runner.invoke(app, ["hypo", "status", hid, "supported", "--confidence", "0.9"])
    assert upd.exit_code == 0
    assert "supported" in upd.stdout

    shown = runner.invoke(app, ["hypo", "show", hid])
    assert shown.exit_code == 0
    assert "Sparse attention" in shown.stdout


def test_hypo_status_rejects_bad_value():
    new = runner.invoke(app, ["hypo", "new", "x"])
    hid = _first_id(new.stdout)
    bad = runner.invoke(app, ["hypo", "status", hid, "totally-bogus"])
    assert bad.exit_code != 0


def test_source_reindex_and_claim_evidence_via_cli(monkeypatch):
    from omni.config.paths import get_paths
    from omni.memory.library import add_papers

    paths = get_paths()
    paths.ensure_dirs()
    add_papers(paths.library, [{
        "arxiv_id": "2310.06825", "title": "Mistral 7B",
        "authors": ["Albert Jiang"], "published": "2023-10-10",
    }])

    reindexed = runner.invoke(app, ["source", "reindex"])
    assert reindexed.exit_code == 0, reindexed.stdout
    assert "Imported" in reindexed.stdout

    src_list = runner.invoke(app, ["source", "list"])
    assert src_list.exit_code == 0
    assert "Mistral 7B" in src_list.stdout
    source_id = _first_id(src_list.stdout)

    claim = runner.invoke(app, ["claim", "new", "Mistral uses sliding window attention"])
    assert claim.exit_code == 0
    claim_id = _first_id(claim.stdout)

    ev = runner.invoke(app, ["evidence", "add", claim_id, "--source", source_id,
                             "--stance", "supports", "--quote", "sliding window attention"])
    assert ev.exit_code == 0, ev.stdout
    assert "Added evidence" in ev.stdout

    shown = runner.invoke(app, ["claim", "show", claim_id])
    assert shown.exit_code == 0
    assert "supports" in shown.stdout


def test_run_and_source_empty_messages():
    runs = runner.invoke(app, ["run", "list"])
    assert runs.exit_code == 0
    assert "No experiment runs" in runs.stdout

    src = runner.invoke(app, ["source", "list"])
    assert src.exit_code == 0
    assert "The source store is empty" in src.stdout

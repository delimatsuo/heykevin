import ast
import json
import pathlib

from app.services.voice_bakeoff_nonce_ledger import FileBackedNonceLedger


def test_first_admission_succeeds_and_persists(tmp_path):
    ledger_path = tmp_path / "nonce_ledger.json"
    ledger = FileBackedNonceLedger(ledger_path)

    assert ledger.admit(
        nonce_digest="a" * 64,
        approval_id_digest="b" * 64,
        binding_digest="c" * 64,
        epoch=1,
    ) is True
    assert ledger_path.exists()


def test_replayed_nonce_is_rejected(tmp_path):
    ledger_path = tmp_path / "nonce_ledger.json"
    ledger = FileBackedNonceLedger(ledger_path)
    ledger.admit(nonce_digest="a" * 64, approval_id_digest="b" * 64, binding_digest="c" * 64, epoch=1)

    replay_ledger = FileBackedNonceLedger(ledger_path)
    assert replay_ledger.admit(
        nonce_digest="a" * 64,
        approval_id_digest="d" * 64,
        binding_digest="e" * 64,
        epoch=1,
    ) is False


def test_replayed_approval_id_is_rejected_even_with_new_nonce(tmp_path):
    ledger_path = tmp_path / "nonce_ledger.json"
    ledger = FileBackedNonceLedger(ledger_path)
    ledger.admit(nonce_digest="a" * 64, approval_id_digest="b" * 64, binding_digest="c" * 64, epoch=1)

    assert ledger.admit(
        nonce_digest="f" * 64,
        approval_id_digest="b" * 64,
        binding_digest="c" * 64,
        epoch=1,
    ) is False


def test_same_binding_different_epoch_is_allowed(tmp_path):
    ledger_path = tmp_path / "nonce_ledger.json"
    ledger = FileBackedNonceLedger(ledger_path)
    ledger.admit(nonce_digest="a" * 64, approval_id_digest="b" * 64, binding_digest="c" * 64, epoch=1)

    assert ledger.admit(
        nonce_digest="f" * 64,
        approval_id_digest="g" * 64,
        binding_digest="c" * 64,
        epoch=2,
    ) is True


def test_same_binding_same_epoch_different_approval_is_rejected(tmp_path):
    ledger_path = tmp_path / "nonce_ledger.json"
    ledger = FileBackedNonceLedger(ledger_path)
    ledger.admit(nonce_digest="a" * 64, approval_id_digest="b" * 64, binding_digest="c" * 64, epoch=1)

    assert ledger.admit(
        nonce_digest="f" * 64,
        approval_id_digest="g" * 64,
        binding_digest="c" * 64,
        epoch=1,
    ) is False


def test_corrupted_ledger_fails_closed(tmp_path):
    ledger_path = tmp_path / "nonce_ledger.json"
    # Simulate a prior writer killed mid-write: a truncated/invalid JSON
    # payload left on disk instead of a complete document.
    ledger_path.write_text('{"consumed_nonces": ["a', encoding="utf-8")
    ledger = FileBackedNonceLedger(ledger_path)

    assert ledger.admit(
        nonce_digest="a" * 64,
        approval_id_digest="b" * 64,
        binding_digest="c" * 64,
        epoch=1,
    ) is False


def test_admission_writes_are_atomic_and_leave_no_temp_files(tmp_path):
    ledger_path = tmp_path / "nonce_ledger.json"
    ledger = FileBackedNonceLedger(ledger_path)

    ledger.admit(nonce_digest="a" * 64, approval_id_digest="b" * 64, binding_digest="c" * 64, epoch=1)
    ledger.admit(nonce_digest="f" * 64, approval_id_digest="g" * 64, binding_digest="c" * 64, epoch=2)

    on_disk = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert on_disk["consumed_nonces"] == ["a" * 64, "f" * 64]
    # The temp-file+os.replace() swap must never leave a stray temp file
    # behind in the ledger directory.
    assert [p.name for p in tmp_path.iterdir()] == [ledger_path.name]


def test_module_performs_no_network_or_subprocess_calls():
    source = pathlib.Path("app/services/voice_bakeoff_nonce_ledger.py").read_text()
    tree = ast.parse(source)
    banned = {"socket", "subprocess", "urllib", "httpx", "requests"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in banned, alias.name
        if isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split(".")[0] not in banned, node.module

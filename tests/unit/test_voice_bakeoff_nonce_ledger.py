import ast
import json
import pathlib
import threading

from app.services import voice_bakeoff_nonce_ledger
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
    # behind in the ledger directory. The only files present should be the
    # ledger itself and its never-replaced sidecar lock file (used to keep
    # fcntl.flock() bound to a stable inode — see FileBackedNonceLedger
    # .__init__ for why the lock is not taken on the ledger file itself).
    assert {p.name for p in tmp_path.iterdir()} == {
        ledger_path.name,
        f"{ledger_path.name}.lock",
    }


def test_concurrent_admits_do_not_lose_committed_writes(tmp_path, monkeypatch):
    """Regression test for the flock/os.replace inode-mismatch race.

    admit() locks a sidecar `<ledger>.lock` file and only ever re-reads the
    ledger content fresh (after acquiring that lock) from self._path. Before
    that fix, admit() flocked the content file itself; since
    _write_atomic() replaces that file's directory entry with a brand-new
    inode on every successful write, a second caller that had already
    opened its handle on the OLD inode before the first caller's replace —
    and was blocked on flock() waiting — would, after unblocking, still
    read through the old, orphaned inode: a stale, pre-replace snapshot
    that misses the first caller's just-committed write. Its own atomic
    replace would then silently overwrite that committed admission.

    This test forces exactly that interleaving deterministically, using
    only threading.Event handshakes (no time.sleep-based timing):

      1. Thread A calls admit(). We intercept its LOCK_EX flock() call,
         let it actually acquire the OS-level lock, then signal
         `a_holds_lock` and block thread A there — still holding the
         lock, before it has read or written anything.
      2. The test driver only starts thread B after observing
         `a_holds_lock`, so thread B's admit() call is guaranteed to find
         the lock held when it calls flock() — that call blocks in the
         kernel until thread A releases the lock.
      3. Thread B's intercepted flock() call sets `b_called_flock`
         immediately before making that (blocking) real call.
      4. Thread A, which was waiting on `b_called_flock`, wakes up and
         proceeds: it reads the (still-empty) ledger, computes its new
         state, atomically replaces the ledger file (a brand-new inode),
         and releases the lock.
      5. Thread B's blocked flock() call now succeeds. Per the fix, it
         must re-read self._path fresh — observing the NEW inode with
         thread A's committed nonce — rather than a stale, pre-replace
         snapshot.

    Both admit() calls use distinct, non-conflicting
    (nonce_digest, approval_id_digest, binding_digest, epoch) tuples, so
    both must return True. The assertion that matters is that BOTH nonces
    are actually present in the final on-disk ledger state — i.e. no lost
    update.
    """
    ledger_path = tmp_path / "nonce_ledger.json"
    ledger_a = FileBackedNonceLedger(ledger_path)
    ledger_b = FileBackedNonceLedger(ledger_path)

    a_holds_lock = threading.Event()
    b_called_flock = threading.Event()

    real_flock = voice_bakeoff_nonce_ledger.fcntl.flock
    lock_ex = voice_bakeoff_nonce_ledger.fcntl.LOCK_EX
    call_count = {"n": 0}
    call_count_lock = threading.Lock()

    def instrumented_flock(fd, operation):
        if operation != lock_ex:
            return real_flock(fd, operation)

        with call_count_lock:
            call_count["n"] += 1
            call_number = call_count["n"]

        if call_number == 1:
            # Thread A: acquire for real, announce it, then block here —
            # still holding the lock — until thread B has issued its own
            # flock() call. Thread B cannot exist yet at this point (the
            # driver only starts it after observing a_holds_lock below),
            # so "call_number == 1" is unambiguously thread A.
            real_flock(fd, operation)
            a_holds_lock.set()
            assert b_called_flock.wait(timeout=5), (
                "thread B never attempted to acquire the lock"
            )
            return None
        else:
            # Thread B: by construction thread A still holds the lock at
            # this point, so this call is guaranteed to block in the
            # kernel until thread A releases it below.
            b_called_flock.set()
            return real_flock(fd, operation)

    monkeypatch.setattr(voice_bakeoff_nonce_ledger.fcntl, "flock", instrumented_flock)

    results: dict[str, bool] = {}
    errors: list[BaseException] = []

    def run_a():
        try:
            results["a"] = ledger_a.admit(
                nonce_digest="1" * 64,
                approval_id_digest="2" * 64,
                binding_digest="3" * 64,
                epoch=1,
            )
        except BaseException as exc:  # noqa: BLE001 - surfaced via `errors`
            errors.append(exc)

    def run_b():
        try:
            results["b"] = ledger_b.admit(
                nonce_digest="4" * 64,
                approval_id_digest="5" * 64,
                binding_digest="6" * 64,
                epoch=1,
            )
        except BaseException as exc:  # noqa: BLE001 - surfaced via `errors`
            errors.append(exc)

    thread_a = threading.Thread(target=run_a)
    thread_b = threading.Thread(target=run_b)

    thread_a.start()
    assert a_holds_lock.wait(timeout=5), "thread A never acquired the lock"
    thread_b.start()

    thread_a.join(timeout=5)
    thread_b.join(timeout=5)

    assert not thread_a.is_alive(), "thread A did not finish"
    assert not thread_b.is_alive(), "thread B did not finish"
    if errors:
        raise errors[0]

    assert results["a"] is True
    assert results["b"] is True

    on_disk = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert set(on_disk["consumed_nonces"]) == {"1" * 64, "4" * 64}
    assert set(on_disk["consumed_approval_ids"]) == {"2" * 64, "5" * 64}


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

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mcp4chatgpt import local_ops
from mcp4chatgpt.workspace import recovery


def make_config(tmp: Path) -> SimpleNamespace:
    root = tmp / "root"
    root.mkdir()
    return SimpleNamespace(
        allowed_roots=[root],
        audit_log=tmp / "logs" / "audit.jsonl",
        max_output_chars=10_000,
    )


class LocalFileTransactionTests(unittest.TestCase):
    def test_cas_conflict_preserves_current_file_and_journals_commit(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            path = config.allowed_roots[0] / "note.txt"

            created = local_ops.write_file(config, str(path), "first", overwrite=False)
            observed = local_ops.read_text(config, str(path))
            self.assertEqual(observed["sha256"], created["sha256"])
            self.assertEqual(observed["bytes"], 5)

            updated = local_ops.write_file(
                config,
                str(path),
                "second",
                overwrite=True,
                expected_sha256=observed["sha256"],
                full_replace_token=observed["full_replace_token"],
            )
            self.assertEqual(updated["before_sha256"], observed["sha256"])
            self.assertEqual(path.read_text(encoding="utf-8"), "second")
            self.assertEqual(
                Path(updated["recovery_blob"]).read_text(encoding="utf-8"),
                "first",
            )

            with self.assertRaisesRegex(ValueError, "file_changed_since_read"):
                local_ops.write_file(
                    config,
                    str(path),
                    "stale overwrite",
                    overwrite=True,
                    expected_sha256=observed["sha256"],
                    full_replace_token=observed["full_replace_token"],
                )
            self.assertEqual(path.read_text(encoding="utf-8"), "second")

            journal = config.audit_log.parent / "file_transactions.jsonl"
            records = [
                json.loads(line)
                for line in journal.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            states = [
                item["state"]
                for item in records
                if item["transaction_id"] == updated["transaction_id"]
            ]
            self.assertEqual(states, ["prepared", "committed"])

    def test_git_recovery_ref_pins_before_and_after_without_touching_index(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            root = config.allowed_roots[0]
            subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
            path = root / "note.txt"
            path.write_text("before", encoding="utf-8")

            observed = local_ops.read_text(config, str(path))
            updated = local_ops.apply_patch(
                config,
                str(path),
                "before",
                "after",
                expected_sha256=observed["sha256"],
            )

            self.assertEqual(path.read_text(encoding="utf-8"), "after")
            self.assertEqual(updated["git_repo_root"], str(root.resolve()))
            self.assertTrue(updated["git_blob"])
            self.assertTrue(updated["git_after_blob"])
            self.assertTrue(
                updated["git_recovery_ref"].startswith(
                    "refs/mcp4chatgpt/file-transactions/"
                )
            )

            resolved_ref = subprocess.run(
                ["git", "-C", str(root), "rev-parse", updated["git_recovery_ref"]],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(resolved_ref, updated["git_recovery_commit"])

            before = subprocess.run(
                ["git", "-C", str(root), "show", f"{resolved_ref}:before"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            after = subprocess.run(
                ["git", "-C", str(root), "show", f"{resolved_ref}:after"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertEqual(before, "before")
            self.assertEqual(after, "after")

            index_entries = subprocess.run(
                ["git", "-C", str(root), "ls-files", "--stage"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertEqual(index_entries, "")

    def test_git_finalize_failure_rolls_back_target(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            root = config.allowed_roots[0]
            subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
            path = root / "note.txt"
            path.write_text("before", encoding="utf-8")
            observed = local_ops.read_text(config, str(path))

            with mock.patch.object(
                recovery,
                "finalize_git_recovery_ref",
                side_effect=RuntimeError("git finalize failed"),
            ):
                with self.assertRaisesRegex(RuntimeError, "git finalize failed"):
                    local_ops.write_file(
                        config,
                        str(path),
                        "after",
                        overwrite=True,
                        expected_sha256=observed["sha256"],
                        full_replace_token=observed["full_replace_token"],
                    )

            self.assertEqual(path.read_text(encoding="utf-8"), "before")
            journal = config.audit_log.parent / "file_transactions.jsonl"
            last = json.loads(journal.read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(last["state"], "aborted")
            self.assertTrue(last["rolled_back"])

    def test_committed_file_survives_commit_journal_failure(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            path = config.allowed_roots[0] / "note.txt"
            path.write_text("before", encoding="utf-8")
            observed = local_ops.read_text(config, str(path))

            with mock.patch.object(
                recovery,
                "append_transaction_record",
                side_effect=[None, OSError("journal unavailable")],
            ):
                updated = local_ops.write_file(
                    config,
                    str(path),
                    "after",
                    overwrite=True,
                    expected_sha256=observed["sha256"],
                    full_replace_token=observed["full_replace_token"],
                )

            self.assertEqual(path.read_text(encoding="utf-8"), "after")
            self.assertEqual(updated["journal_state"], "commit_record_failed")
            self.assertIn("journal unavailable", updated["journal_warning"])

    def test_replace_then_directory_fsync_failure_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            path = config.allowed_roots[0] / "note.txt"
            path.write_text("before", encoding="utf-8")
            observed = local_ops.read_text(config, str(path))
            real_fsync = recovery.fsync_directory
            failed_once = False

            def fail_target_dir(directory):
                nonlocal failed_once
                if Path(directory).resolve() == path.parent.resolve():
                    if not failed_once:
                        failed_once = True
                        raise OSError("injected target directory fsync failure")
                return real_fsync(directory)

            with mock.patch.object(recovery, "fsync_directory", side_effect=fail_target_dir):
                with self.assertRaisesRegex(OSError, "fsync failure"):
                    local_ops.apply_patch(config, str(path), "before", "after", expected_sha256=observed["sha256"])
            self.assertEqual(path.read_text(encoding="utf-8"), "before")
            records = [json.loads(line) for line in (config.audit_log.parent / "file_transactions.jsonl").read_text().splitlines()]
            self.assertEqual(records[-1]["state"], "aborted")
            self.assertTrue(records[-1]["rolled_back"])

    def test_rollback_failure_returns_transaction_id_and_unknown_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            path = config.allowed_roots[0] / "note.txt"
            path.write_text("before", encoding="utf-8")
            observed = local_ops.read_text(config, str(path))
            real_fsync = recovery.fsync_directory
            target_failures = 0

            def fail_both_target_syncs(directory):
                nonlocal target_failures
                if Path(directory).resolve() == path.parent.resolve():
                    target_failures += 1
                    if target_failures <= 2:
                        raise OSError("injected target sync failure")
                return real_fsync(directory)

            with mock.patch.object(recovery, "fsync_directory", side_effect=fail_both_target_syncs):
                with self.assertRaisesRegex(RuntimeError, r"filetx-.*outcome_unknown; rollback failed"):
                    local_ops.apply_patch(config, str(path), "before", "after", expected_sha256=observed["sha256"])
            self.assertEqual(target_failures, 2)
            self.assertEqual(path.read_text(encoding="utf-8"), "before")

    def test_abort_journal_failure_returns_transaction_id_and_unknown_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            path = config.allowed_roots[0] / "note.txt"
            path.write_text("before", encoding="utf-8")
            observed = local_ops.read_text(config, str(path))
            real_fsync = recovery.fsync_directory
            failed = False

            def fail_target_once(directory):
                nonlocal failed
                if Path(directory).resolve() == path.parent.resolve() and not failed:
                    failed = True
                    raise OSError("injected target sync failure")
                return real_fsync(directory)

            with (
                mock.patch.object(recovery, "fsync_directory", side_effect=fail_target_once),
                mock.patch.object(recovery, "append_transaction_record", side_effect=[None, OSError("journal failed")]),
            ):
                with self.assertRaisesRegex(RuntimeError, r"filetx-.*outcome_unknown; abort journal failed"):
                    local_ops.apply_patch(config, str(path), "before", "after", expected_sha256=observed["sha256"])
            self.assertEqual(path.read_text(encoding="utf-8"), "before")

    def test_truncated_read_cannot_authorize_whole_file_replace(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            path = config.allowed_roots[0] / "large.txt"
            path.write_text("x" * 12_000, encoding="utf-8")

            partial = local_ops.read_text(config, str(path), max_chars=16)
            self.assertTrue(partial["truncated"])
            self.assertFalse(partial["full_replace_eligible"])
            self.assertIsNone(partial["full_replace_token"])
            self.assertEqual(partial["full_replace_blocked_reason"], "truncated")

            with self.assertRaisesRegex(
                ValueError,
                "full_replace_requires_complete_read",
            ):
                local_ops.write_file(
                    config,
                    str(path),
                    "y" * 12_000,
                    overwrite=True,
                    expected_sha256=partial["sha256"],
                    full_replace_token=partial["full_replace_token"],
                )

            complete = local_ops.read_text(config, str(path), max_chars=20_000)
            self.assertTrue(complete["full_replace_eligible"])
            updated = local_ops.write_file(
                config,
                str(path),
                "y" * 12_000,
                overwrite=True,
                expected_sha256=complete["sha256"],
                full_replace_token=complete["full_replace_token"],
            )
            self.assertEqual(updated["journal_state"], "committed")
            self.assertEqual(path.read_text(encoding="utf-8"), "y" * 12_000)

    def test_whole_file_shrink_requires_explicit_override(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            path = config.allowed_roots[0] / "large.txt"
            path.write_text("x" * 12_000, encoding="utf-8")
            observed = local_ops.read_text(config, str(path), max_chars=20_000)

            with self.assertRaisesRegex(ValueError, "suspicious_file_shrink"):
                local_ops.write_file(
                    config,
                    str(path),
                    "small",
                    overwrite=True,
                    expected_sha256=observed["sha256"],
                    full_replace_token=observed["full_replace_token"],
                )
            self.assertEqual(path.read_text(encoding="utf-8"), "x" * 12_000)

            updated = local_ops.write_file(
                config,
                str(path),
                "small",
                overwrite=True,
                expected_sha256=observed["sha256"],
                full_replace_token=observed["full_replace_token"],
                allow_large_reduction=True,
            )
            self.assertEqual(updated["validation"]["validated"], True)
            self.assertEqual(path.read_text(encoding="utf-8"), "small")

    def test_invalid_python_candidate_is_rejected_before_file_creation(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            path = config.allowed_roots[0] / "broken.py"

            with self.assertRaisesRegex(
                ValueError,
                "candidate_python_syntax_error",
            ):
                local_ops.write_file(
                    config,
                    str(path),
                    "def broken(:\n    pass\n",
                    overwrite=False,
                )
            self.assertFalse(path.exists())
            self.assertFalse(
                (config.audit_log.parent / "file_transactions.jsonl").exists()
            )

    def test_cua_backend_contract_rejects_missing_functions(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            path = config.allowed_roots[0] / "computer_cua_backend.py"
            candidate = (
                "def supports(operation, args):\n"
                "    return True\n"
                "\n"
                "def call(operation, args, effectful=False):\n"
                "    return {}\n"
            )

            with self.assertRaisesRegex(
                ValueError,
                "computer_cua_backend_contract_missing",
            ):
                local_ops.write_file(
                    config,
                    str(path),
                    candidate,
                    overwrite=False,
                )
            self.assertFalse(path.exists())

    def test_cua_supports_rejects_action_execution_calls(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            path = config.allowed_roots[0] / "computer_cua_backend.py"
            candidate = """
def supports(operation, args):
    return _call_js("bad")

def call(operation, args, effectful=False):
    return {}

def stop():
    return None

def _parse_elements(*args):
    return []

def _image_metadata(*args):
    return {}
""".lstrip()

            with self.assertRaisesRegex(
                ValueError,
                "computer_cua_supports_not_pure",
            ):
                local_ops.write_file(
                    config,
                    str(path),
                    candidate,
                    overwrite=False,
                )
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()

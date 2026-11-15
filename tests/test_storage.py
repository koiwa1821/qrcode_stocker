import hashlib
import os
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from api.domain import ApiError, to_iso
from api.storage import SQLiteStore


class SQLiteStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = SQLiteStore(os.path.join(self.temp_dir.name, "qr.sqlite3"))
        self.now = datetime(2026, 11, 15, 8, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.temp_dir.cleanup()

    def register(self, email="user@example.com", sid=None):
        browser_id = sid or str(uuid.uuid4())
        code = "123456"
        salt = "test-salt"
        owner_id = str(uuid.uuid4())
        self.store.create_challenge(
            {
                "email": email,
                "code_hash": hashlib.sha256((salt + code).encode("utf-8")).hexdigest(),
                "code_salt": salt,
                "action": "register",
                "owner_id": owner_id,
                "sid": browser_id,
                "expires_at": to_iso(self.now + timedelta(minutes=10)),
            },
            self.now,
        )
        token_hash = hashlib.sha256(uuid.uuid4().hex.encode("utf-8")).hexdigest()
        if not hasattr(self, "session_tokens"):
            self.session_tokens = {}
        self.session_tokens[browser_id] = token_hash
        authenticated_owner = self.store.verify_email(
            email,
            browser_id,
            code,
            token_hash,
            to_iso(self.now + timedelta(days=30)),
            self.now,
        )
        self.assertEqual(owner_id, authenticated_owner)
        return owner_id, browser_id

    def issue_login_code(self, email, owner_id, sid, code="654321"):
        salt = "login-salt"
        self.store.create_challenge(
            {
                "email": email,
                "code_hash": hashlib.sha256((salt + code).encode("utf-8")).hexdigest(),
                "code_salt": salt,
                "action": "login",
                "owner_id": owner_id,
                "sid": sid,
                "expires_at": to_iso(self.now + timedelta(minutes=10)),
            },
            self.now + timedelta(seconds=31),
        )
        return code

    @staticmethod
    def record(record_id, content, read_at, note=""):
        return {
            "id": record_id,
            "content": content,
            "read_at": to_iso(read_at),
            "note": note,
        }

    def seed_records(self, owner_id, count, read_at=None, origin_paid=False, state="active"):
        start = read_at or (self.now - timedelta(days=5))
        connection = self.store._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany(
                """
                INSERT INTO records(
                    owner_id, id, content, read_at, note, state, origin_paid,
                    retained_until, created_at, updated_at, deleted_at
                ) VALUES (?, ?, ?, ?, '', ?, ?, NULL, ?, ?, NULL)
                """,
                [
                    (
                        owner_id,
                        str(uuid.UUID(int=index + 1, version=4)),
                        f"seed-{index}",
                        to_iso(start + timedelta(seconds=index)),
                        state,
                        int(origin_paid),
                        to_iso(self.now),
                        to_iso(self.now),
                    )
                    for index in range(count)
                ],
            )
            connection.commit()
        finally:
            connection.close()

    def test_guest_free_and_paid_limits_accept_new_records_and_prune_oldest(self):
        guest_sid = str(uuid.uuid4())
        guest_owner = f"guest:{guest_sid}"
        self.store.ensure_guest(guest_owner, self.now)
        self.seed_records(guest_owner, 100, read_at=self.now - timedelta(days=1))
        guest_result = self.store.create_record(
            guest_owner,
            self.record(str(uuid.UUID(int=90001, version=4)), "new guest", self.now),
            self.now,
        )
        self.assertEqual(100, guest_result["active_count"])
        self.assertEqual(100, guest_result["limit"])
        self.assertEqual(str(uuid.UUID(int=1, version=4)), guest_result["logical_deleted"][0]["id"])
        self.assertEqual("active", guest_result["record"]["state"])

        free_owner, _ = self.register("free@example.com")
        self.seed_records(free_owner, 500, read_at=self.now - timedelta(days=1))
        free_result = self.store.create_record(
            free_owner,
            self.record(str(uuid.UUID(int=90002, version=4)), "new free", self.now),
            self.now,
        )
        self.assertEqual(500, free_result["active_count"])
        self.assertEqual(500, free_result["limit"])
        self.assertEqual(str(uuid.UUID(int=1, version=4)), free_result["logical_deleted"][0]["id"])

        paid_owner, _ = self.register("paid@example.com")
        self.store.set_subscription(
            paid_owner,
            "paid-start",
            int(self.now.timestamp()),
            "active",
            to_iso(self.now + timedelta(days=30)),
            now=self.now,
        )
        self.seed_records(paid_owner, 10000, read_at=self.now - timedelta(days=30), origin_paid=True)
        paid_result = self.store.create_record(
            paid_owner,
            self.record(str(uuid.UUID(int=90003, version=4)), "new paid", self.now),
            self.now,
        )
        self.assertEqual(10000, paid_result["active_count"])
        self.assertEqual(10000, paid_result["limit"])
        self.assertEqual(str(uuid.UUID(int=1, version=4)), paid_result["logical_deleted"][0]["id"])

    def test_deleted_rows_are_hidden_from_search_and_excluded_from_limit_without_physical_delete(self):
        sid = str(uuid.uuid4())
        owner_id = f"guest:{sid}"
        self.store.ensure_guest(owner_id, self.now)
        record_id = str(uuid.uuid4())
        self.store.create_record(
            owner_id,
            self.record(record_id, "a private QR value", self.now),
            self.now,
        )
        deleted = self.store.delete_record(owner_id, record_id, self.now)
        result = self.store.list_records(owner_id, "private", None, 50, self.now)
        self.assertTrue(deleted["deleted"])
        self.assertEqual(0, deleted["active_count"])
        self.assertEqual(0, result["active_count"])
        self.assertEqual([], result["records"])

        connection = self.store._connect()
        try:
            row = connection.execute(
                "SELECT state FROM records WHERE owner_id = ? AND id = ?",
                (owner_id, record_id),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual("deleted", row["state"])

    def test_guest_records_and_offline_local_rows_transfer_idempotently_after_registration(self):
        sid = str(uuid.uuid4())
        guest_owner = f"guest:{sid}"
        self.store.ensure_guest(guest_owner, self.now)
        saved_id = str(uuid.uuid4())
        self.store.create_record(
            guest_owner,
            self.record(saved_id, "already synchronized", self.now - timedelta(hours=2), "server"),
            self.now,
        )
        account_id, _ = self.register("transfer@example.com")
        pending_id = str(uuid.uuid4())
        transfer = self.store.transfer_guest_records(
            sid,
            account_id,
            [
                self.record(pending_id, "offline record", self.now - timedelta(hours=1), "local"),
            ],
            self.now,
        )
        self.assertCountEqual([saved_id, pending_id], transfer["migrated_ids"])
        self.assertEqual(2, transfer["active_count"])
        self.assertEqual(0, self.store.account_summary(guest_owner, self.now)["active_count"])

        repeated = self.store.transfer_guest_records(sid, account_id, [], self.now)
        self.assertCountEqual([saved_id], repeated["migrated_ids"])
        self.assertEqual(2, repeated["active_count"])
        result = self.store.list_records(account_id, "", None, 50, self.now)
        self.assertEqual({saved_id, pending_id}, {item["id"] for item in result["records"]})

    def test_browser_limit_rejects_new_login_without_evicting_and_paid_expands_it(self):
        email = "devices@example.com"
        owner_id, first_sid = self.register(email)
        second_sid = str(uuid.uuid4())
        second_code = self.issue_login_code(email, owner_id, second_sid)
        second_token_hash = hashlib.sha256(b"second-token").hexdigest()
        self.store.verify_email(
            email,
            second_sid,
            second_code,
            second_token_hash,
            to_iso(self.now + timedelta(days=30)),
            self.now + timedelta(seconds=31),
        )
        self.session_tokens[second_sid] = second_token_hash
        third_sid = str(uuid.uuid4())
        third_code = self.issue_login_code(email, owner_id, third_sid)
        with self.assertRaises(ApiError) as context:
            self.store.verify_email(
                email,
                third_sid,
                third_code,
                hashlib.sha256(b"third-token").hexdigest(),
                to_iso(self.now + timedelta(days=30)),
                self.now + timedelta(seconds=62),
            )
        self.assertEqual("device_limit_reached", context.exception.code)
        self.assertEqual(
            2,
            self.store.account_summary(owner_id, self.now + timedelta(seconds=62))["device_count"],
        )
        self.assertEqual(
            owner_id,
            self.store.authenticate(self.session_tokens[first_sid], first_sid, self.now),
        )
        self.assertEqual(
            owner_id,
            self.store.authenticate(second_token_hash, second_sid, self.now),
        )

        self.store.set_subscription(
            owner_id,
            "paid-device-limit",
            int(self.now.timestamp()) + 1,
            "active",
            to_iso(self.now + timedelta(days=30)),
            now=self.now + timedelta(seconds=63),
        )
        self.store.verify_email(
            email,
            third_sid,
            third_code,
            hashlib.sha256(b"third-token").hexdigest(),
            to_iso(self.now + timedelta(days=30)),
            self.now + timedelta(seconds=64),
        )
        self.session_tokens[third_sid] = hashlib.sha256(b"third-token").hexdigest()
        summary = self.store.account_summary(owner_id, self.now + timedelta(seconds=64))
        self.assertEqual(3, summary["device_count"])

    def test_expired_browser_cannot_bypass_free_browser_limit_when_reauthenticating(self):
        email = "expired-device@example.com"
        owner_id, expired_sid = self.register(email)
        self.store.set_subscription(
            owner_id,
            "temporary-paid",
            int(self.now.timestamp()) + 1,
            "active",
            to_iso(self.now + timedelta(days=30)),
            now=self.now,
        )

        active_sids = [str(uuid.uuid4()), str(uuid.uuid4())]
        for index, sid in enumerate(active_sids):
            code = self.issue_login_code(email, owner_id, sid, code=f"65432{index}")
            self.store.verify_email(
                email,
                sid,
                code,
                hashlib.sha256(f"token-{index}".encode("utf-8")).hexdigest(),
                to_iso(self.now + timedelta(days=30)),
                self.now + timedelta(seconds=40 + index),
            )

        free_at = self.now + timedelta(seconds=50)
        self.store.set_subscription(
            owner_id,
            "temporary-paid-ended",
            int(free_at.timestamp()),
            "canceled",
            to_iso(free_at),
            now=free_at,
        )
        connection = self.store._connect()
        try:
            connection.execute(
                "UPDATE devices SET expires_at = ? WHERE owner_id = ? AND sid = ?",
                (to_iso(free_at - timedelta(seconds=1)), owner_id, expired_sid),
            )
            connection.commit()
        finally:
            connection.close()

        retry_at = free_at + timedelta(seconds=1)
        retry_code = self.issue_login_code(email, owner_id, expired_sid)
        with self.assertRaises(ApiError) as context:
            self.store.verify_email(
                email,
                expired_sid,
                retry_code,
                hashlib.sha256(b"expired-device-token").hexdigest(),
                to_iso(retry_at + timedelta(days=30)),
                retry_at,
            )
        self.assertEqual("device_limit_reached", context.exception.code)

    def test_concurrent_guest_saves_are_serialized_at_the_limit(self):
        sid = str(uuid.uuid4())
        owner_id = f"guest:{sid}"
        self.store.ensure_guest(owner_id, self.now)
        self.seed_records(owner_id, 100, read_at=self.now - timedelta(days=1))

        def save(index):
            return self.store.create_record(
                owner_id,
                self.record(
                    str(uuid.UUID(int=100000 + index, version=4)),
                    f"concurrent-{index}",
                    self.now + timedelta(seconds=index),
                ),
                self.now,
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(save, range(8)))

        self.assertTrue(all(result["record"] for result in results))
        self.assertEqual(100, self.store.account_summary(owner_id, self.now)["active_count"])
        connection = self.store._connect()
        try:
            states = connection.execute(
                """
                SELECT state, COUNT(*) AS count FROM records
                WHERE owner_id = ? GROUP BY state
                """,
                (owner_id,),
            ).fetchall()
        finally:
            connection.close()
        counts = {row["state"]: row["count"] for row in states}
        self.assertEqual(108, sum(counts.values()))
        self.assertEqual(8, counts["deleted"])
        self.assertEqual(100, counts["active"])

    def test_paid_end_retains_data_and_recontract_prunes_old_paid_rows_over_10000(self):
        owner_id, _ = self.register("retention@example.com")
        self.store.set_subscription(
            owner_id,
            "retention-paid-start",
            int(self.now.timestamp()),
            "active",
            to_iso(self.now + timedelta(days=30)),
            now=self.now,
        )
        self.seed_records(
            owner_id,
            10000,
            read_at=self.now - timedelta(days=60),
            origin_paid=True,
        )
        ended_at = self.now + timedelta(minutes=1)
        ended = self.store.set_subscription(
            owner_id,
            "retention-paid-end",
            int(ended_at.timestamp()),
            "canceled",
            to_iso(ended_at),
            now=ended_at,
        )
        self.assertEqual(0, ended["active_count"])
        summary = self.store.account_summary(owner_id, ended_at)
        self.assertEqual(0, summary["active_count"])

        retained = 0
        connection = self.store._connect()
        try:
            retained = connection.execute(
                "SELECT COUNT(*) AS count FROM records WHERE owner_id = ? AND state = 'retained'",
                (owner_id,),
            ).fetchone()["count"]
        finally:
            connection.close()
        self.assertEqual(10000, retained)

        for index in range(500):
            self.store.create_record(
                owner_id,
                self.record(
                    str(uuid.UUID(int=20000 + index, version=4)),
                    f"free-period-{index}",
                    ended_at + timedelta(seconds=index),
                ),
                ended_at,
            )
        self.assertEqual(500, self.store.account_summary(owner_id, ended_at)["active_count"])

        reactivated_at = ended_at + timedelta(days=30)
        restored = self.store.set_subscription(
            owner_id,
            "retention-recontract",
            int(reactivated_at.timestamp()),
            "active",
            to_iso(reactivated_at + timedelta(days=30)),
            now=reactivated_at,
        )
        self.assertEqual(500, len(restored["logical_deleted"]))
        self.assertEqual(10000, restored["active_count"])
        self.assertEqual(
            str(uuid.UUID(int=1, version=4)),
            restored["logical_deleted"][0]["id"],
        )
        connection = self.store._connect()
        try:
            deleted_count = connection.execute(
                "SELECT COUNT(*) AS count FROM records WHERE owner_id = ? AND state = 'deleted'",
                (owner_id,),
            ).fetchone()["count"]
            retained_count = connection.execute(
                "SELECT COUNT(*) AS count FROM records WHERE owner_id = ? AND state = 'retained'",
                (owner_id,),
            ).fetchone()["count"]
        finally:
            connection.close()
        self.assertEqual(500, deleted_count)
        self.assertEqual(0, retained_count)

    def test_exact_recontract_limit_restores_records_but_expired_retention_stays_hidden(self):
        owner_id, _ = self.register("exact-limit@example.com")
        self.store.set_subscription(
            owner_id,
            "exact-paid-start",
            int(self.now.timestamp()),
            "active",
            to_iso(self.now + timedelta(days=30)),
            now=self.now,
        )
        self.seed_records(
            owner_id,
            10000,
            read_at=self.now - timedelta(days=90),
            origin_paid=True,
        )
        ended_at = self.now + timedelta(minutes=1)
        self.store.set_subscription(
            owner_id,
            "exact-paid-end",
            int(ended_at.timestamp()),
            "canceled",
            to_iso(ended_at),
            now=ended_at,
        )
        connection = self.store._connect()
        try:
            connection.execute(
                """
                UPDATE records SET retained_until = ?
                WHERE owner_id = ? AND id = ?
                """,
                (
                    to_iso(ended_at - timedelta(seconds=1)),
                    owner_id,
                    str(uuid.UUID(int=1, version=4)),
                ),
            )
            connection.commit()
        finally:
            connection.close()

        reactivated_at = ended_at + timedelta(days=30)
        result = self.store.set_subscription(
            owner_id,
            "exact-recontract",
            int(reactivated_at.timestamp()),
            "active",
            to_iso(reactivated_at + timedelta(days=30)),
            now=reactivated_at,
        )
        self.assertEqual([], result["logical_deleted"])
        self.assertEqual(9999, result["active_count"])
        connection = self.store._connect()
        try:
            row = connection.execute(
                "SELECT state FROM records WHERE owner_id = ? AND id = ?",
                (owner_id, str(uuid.UUID(int=1, version=4))),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual("retained", row["state"])


if __name__ == "__main__":
    unittest.main()

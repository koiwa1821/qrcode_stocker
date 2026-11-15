import hashlib
import hmac
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta

from api.domain import (
    ApiError,
    decode_cursor,
    device_limit,
    encode_cursor,
    is_paid,
    plan_for,
    record_limit,
    to_iso,
    utc_now,
)


class SQLiteStore:
    def __init__(self, database_path):
        self.database_path = database_path
        if database_path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(database_path)), exist_ok=True)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(
            self.database_path,
            timeout=30,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self):
        with self._connection() as connection:
            if self.database_path != ":memory:":
                connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS owners (
                    owner_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL CHECK (kind IN ('guest', 'user')),
                    email TEXT UNIQUE,
                    stripe_customer_id TEXT UNIQUE,
                    subscription_id TEXT,
                    subscription_status TEXT NOT NULL DEFAULT 'free',
                    paid_until TEXT,
                    last_event_id TEXT,
                    last_event_created INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS challenges (
                    email TEXT PRIMARY KEY,
                    code_hash TEXT NOT NULL,
                    code_salt TEXT NOT NULL,
                    action TEXT NOT NULL CHECK (action IN ('register', 'login')),
                    owner_id TEXT NOT NULL,
                    sid TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS devices (
                    owner_id TEXT NOT NULL REFERENCES owners(owner_id),
                    sid TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    PRIMARY KEY (owner_id, sid)
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL REFERENCES owners(owner_id),
                    sid TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_sessions_owner_sid
                    ON sessions(owner_id, sid);
                CREATE TABLE IF NOT EXISTS records (
                    owner_id TEXT NOT NULL REFERENCES owners(owner_id),
                    id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    read_at TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL CHECK (state IN ('active', 'retained', 'deleted')),
                    origin_paid INTEGER NOT NULL DEFAULT 0,
                    retained_until TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    deleted_at TEXT,
                    transferred_to TEXT,
                    PRIMARY KEY (owner_id, id)
                );
                CREATE INDEX IF NOT EXISTS ix_records_owner_state_read
                    ON records(owner_id, state, read_at, id);
                CREATE INDEX IF NOT EXISTS ix_records_owner_state_created
                    ON records(owner_id, state, created_at);
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(records)").fetchall()
            }
            if "transferred_to" not in columns:
                connection.execute("ALTER TABLE records ADD COLUMN transferred_to TEXT")

    @contextmanager
    def _transaction(self):
        connection = self._connect()
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _owner_row(connection, owner_id):
        row = connection.execute(
            "SELECT * FROM owners WHERE owner_id = ?",
            (owner_id,),
        ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _ensure_guest(connection, owner_id, now):
        connection.execute(
            """
            INSERT OR IGNORE INTO owners(owner_id, kind, created_at, updated_at)
            VALUES (?, 'guest', ?, ?)
            """,
            (owner_id, now, now),
        )

    @staticmethod
    def _record_row(row):
        return dict(row) if row else None

    @staticmethod
    def _active_count(connection, owner_id):
        row = connection.execute(
            "SELECT COUNT(*) AS count FROM records WHERE owner_id = ? AND state = 'active'",
            (owner_id,),
        ).fetchone()
        return row["count"]

    @staticmethod
    def _oldest_active(connection, owner_id):
        row = connection.execute(
            """
            SELECT * FROM records
            WHERE owner_id = ? AND state = 'active'
            ORDER BY read_at ASC, id ASC
            LIMIT 1
            """,
            (owner_id,),
        ).fetchone()
        return SQLiteStore._record_row(row)

    @staticmethod
    def _trim_to_limit(connection, owner_id, owner, now):
        maximum = record_limit(owner, now)
        deleted = []
        while SQLiteStore._active_count(connection, owner_id) > maximum:
            oldest = SQLiteStore._oldest_active(connection, owner_id)
            if oldest is None:
                break
            connection.execute(
                """
                UPDATE records
                SET state = 'deleted', deleted_at = ?, updated_at = ?
                WHERE owner_id = ? AND id = ? AND state = 'active'
                """,
                (to_iso(now), to_iso(now), owner_id, oldest["id"]),
            )
            deleted.append({"id": oldest["id"], "readAt": oldest["read_at"]})
        return deleted

    def ensure_guest(self, owner_id, now=None):
        current_time = to_iso(now or utc_now())
        with self._transaction() as connection:
            self._ensure_guest(connection, owner_id, current_time)

    def get_owner_by_email(self, email):
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM owners WHERE email = ? AND kind = 'user'",
                (email,),
            ).fetchone()
            return dict(row) if row else None

    def get_owner_by_customer(self, customer_id):
        if not customer_id:
            return None
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM owners WHERE stripe_customer_id = ? AND kind = 'user'",
                (customer_id,),
            ).fetchone()
            return dict(row) if row else None

    def create_challenge(self, challenge, now=None):
        current_time = now or utc_now()
        now_text = to_iso(current_time)
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT created_at FROM challenges WHERE email = ?",
                (challenge["email"],),
            ).fetchone()
            if existing and current_time - datetime.fromisoformat(
                existing["created_at"].replace("Z", "+00:00")
            ) < timedelta(seconds=30):
                raise ApiError(429, "verification_rate_limited", "確認コードは30秒後に再送できます。")
            connection.execute(
                """
                INSERT INTO challenges(
                    email, code_hash, code_salt, action, owner_id, sid,
                    expires_at, created_at, attempts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(email) DO UPDATE SET
                    code_hash = excluded.code_hash,
                    code_salt = excluded.code_salt,
                    action = excluded.action,
                    owner_id = excluded.owner_id,
                    sid = excluded.sid,
                    expires_at = excluded.expires_at,
                    created_at = excluded.created_at,
                    attempts = 0
                """,
                (
                    challenge["email"],
                    challenge["code_hash"],
                    challenge["code_salt"],
                    challenge["action"],
                    challenge["owner_id"],
                    challenge["sid"],
                    challenge["expires_at"],
                    now_text,
                ),
            )

    def verify_email(self, email, sid, code, token_hash, session_expires_at, now=None):
        current_time = now or utc_now()
        now_text = to_iso(current_time)
        commit_error = None
        owner_id = None
        with self._transaction() as connection:
            challenge = connection.execute(
                "SELECT * FROM challenges WHERE email = ?",
                (email,),
            ).fetchone()
            if challenge is None or challenge["sid"] != sid:
                commit_error = ApiError(400, "invalid_verification", "確認コードを再取得してください。")
            elif challenge["attempts"] >= 5:
                commit_error = ApiError(429, "verification_locked", "確認コードの試行回数を超えました。再取得してください。")
            elif challenge["expires_at"] <= now_text:
                connection.execute("DELETE FROM challenges WHERE email = ?", (email,))
                commit_error = ApiError(400, "verification_expired", "確認コードの有効期限が切れました。再取得してください。")
            else:
                calculated = hashlib.sha256(
                    (challenge["code_salt"] + code).encode("utf-8")
                ).hexdigest()
                if not hmac.compare_digest(calculated, challenge["code_hash"]):
                    connection.execute(
                        "UPDATE challenges SET attempts = attempts + 1 WHERE email = ?",
                        (email,),
                    )
                    commit_error = ApiError(400, "invalid_verification", "確認コードが正しくありません。")
                else:
                    existing = connection.execute(
                        "SELECT * FROM owners WHERE email = ? AND kind = 'user'",
                        (email,),
                    ).fetchone()
                    if challenge["action"] == "register":
                        if existing:
                            commit_error = ApiError(409, "email_exists", "このメールアドレスは登録済みです。ログインしてください。")
                        else:
                            owner_id = challenge["owner_id"]
                            connection.execute(
                                """
                                INSERT INTO owners(owner_id, kind, email, created_at, updated_at)
                                VALUES (?, 'user', ?, ?, ?)
                                """,
                                (owner_id, email, now_text, now_text),
                            )
                    elif existing is None:
                        commit_error = ApiError(404, "account_not_found", "アカウントが見つかりません。先に登録してください。")
                    else:
                        owner_id = existing["owner_id"]

                    if commit_error is None:
                        owner = self._owner_row(connection, owner_id)
                        active_devices = connection.execute(
                            """
                            SELECT COUNT(*) AS count FROM devices
                            WHERE owner_id = ? AND expires_at > ?
                            """,
                            (owner_id, now_text),
                        ).fetchone()["count"]
                        existing_device = connection.execute(
                            "SELECT 1 FROM devices WHERE owner_id = ? AND sid = ? AND expires_at > ?",
                            (owner_id, sid, now_text),
                        ).fetchone()
                        maximum_devices = device_limit(owner, current_time)
                        if existing_device is None and active_devices >= maximum_devices:
                            commit_error = ApiError(409, "device_limit_reached", "認証ブラウザ数の上限に達しています。既存ブラウザをログアウトしてから再試行してください。")
                        else:
                            connection.execute(
                                """
                                INSERT INTO devices(owner_id, sid, created_at, last_seen_at, expires_at)
                                VALUES (?, ?, ?, ?, ?)
                                ON CONFLICT(owner_id, sid) DO UPDATE SET
                                    last_seen_at = excluded.last_seen_at,
                                    expires_at = excluded.expires_at
                                """,
                                (owner_id, sid, now_text, now_text, session_expires_at),
                            )
                            connection.execute(
                                """
                                INSERT INTO sessions(token_hash, owner_id, sid, expires_at)
                                VALUES (?, ?, ?, ?)
                                ON CONFLICT(token_hash) DO UPDATE SET
                                    owner_id = excluded.owner_id,
                                    sid = excluded.sid,
                                    expires_at = excluded.expires_at
                                """,
                                (token_hash, owner_id, sid, session_expires_at),
                            )
                            connection.execute(
                                "DELETE FROM challenges WHERE email = ?",
                                (email,),
                            )
        if commit_error is not None:
            raise commit_error
        return owner_id

    def authenticate(self, token_hash, sid, now=None):
        now_text = to_iso(now or utc_now())
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT sessions.owner_id FROM sessions
                JOIN owners ON owners.owner_id = sessions.owner_id
                WHERE sessions.token_hash = ? AND sessions.sid = ?
                    AND sessions.expires_at > ? AND owners.kind = 'user'
                """,
                (token_hash, sid, now_text),
            ).fetchone()
            return row["owner_id"] if row else None

    def logout(self, token_hash, sid):
        with self._transaction() as connection:
            session = connection.execute(
                "SELECT owner_id FROM sessions WHERE token_hash = ? AND sid = ?",
                (token_hash, sid),
            ).fetchone()
            if session:
                connection.execute(
                    "DELETE FROM sessions WHERE owner_id = ? AND sid = ?",
                    (session["owner_id"], sid),
                )
                connection.execute(
                    "DELETE FROM devices WHERE owner_id = ? AND sid = ?",
                    (session["owner_id"], sid),
                )

    def account_summary(self, owner_id, now=None):
        current_time = to_iso(now or utc_now())
        with self._connection() as connection:
            owner = self._owner_row(connection, owner_id)
            if owner is None:
                raise ApiError(404, "account_not_found", "アカウントが見つかりません。")
            active_count = self._active_count(connection, owner_id)
            device_count = connection.execute(
                "SELECT COUNT(*) AS count FROM devices WHERE owner_id = ? AND expires_at > ?",
                (owner_id, current_time),
            ).fetchone()["count"]
            return {
                "owner": owner,
                "active_count": active_count,
                "device_count": device_count,
            }

    @staticmethod
    def _find_record(connection, owner_id, record_id):
        row = connection.execute(
            "SELECT * FROM records WHERE owner_id = ? AND id = ?",
            (owner_id, record_id),
        ).fetchone()
        return dict(row) if row else None

    def create_record(self, owner_id, record, now=None):
        current_time = now or utc_now()
        now_text = to_iso(current_time)
        with self._transaction() as connection:
            owner = self._owner_row(connection, owner_id)
            if owner is None:
                if owner_id.startswith("guest:"):
                    self._ensure_guest(connection, owner_id, now_text)
                    owner = self._owner_row(connection, owner_id)
                else:
                    raise ApiError(401, "unauthorized", "ログイン状態を確認できません。")
            existing = self._find_record(connection, owner_id, record["id"])
            if existing:
                return {
                    "record": existing,
                    "active_count": self._active_count(connection, owner_id),
                    "limit": record_limit(owner, current_time),
                    "plan": plan_for(owner, current_time),
                    "logical_deleted": [],
                    "duplicate": True,
                }
            connection.execute(
                """
                INSERT INTO records(
                    owner_id, id, content, read_at, note, state, origin_paid,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)
                """,
                (
                    owner_id,
                    record["id"],
                    record["content"],
                    record["read_at"],
                    record["note"],
                    1 if is_paid(owner, current_time) else 0,
                    now_text,
                    now_text,
                ),
            )
            deleted = self._trim_to_limit(connection, owner_id, owner, current_time)
            return {
                "record": self._find_record(connection, owner_id, record["id"]),
                "active_count": self._active_count(connection, owner_id),
                "limit": record_limit(owner, current_time),
                "plan": plan_for(owner, current_time),
                "logical_deleted": deleted,
                "duplicate": False,
            }

    def list_records(self, owner_id, query="", cursor=None, page_size=50, now=None):
        current_time = now or utc_now()
        decoded = decode_cursor(cursor, query)
        clauses = ["owner_id = ?", "state = 'active'"]
        parameters = [owner_id]
        if query:
            clauses.append("(instr(lower(content), lower(?)) > 0 OR instr(lower(note), lower(?)) > 0)")
            parameters.extend([query, query])
        if decoded:
            clauses.append("(read_at > ? OR (read_at = ? AND id > ?))")
            parameters.extend([decoded["read_at"], decoded["read_at"], decoded["id"]])
        parameters.append(page_size + 1)
        with self._connection() as connection:
            owner = self._owner_row(connection, owner_id)
            if owner is None:
                raise ApiError(404, "account_not_found", "アカウントが見つかりません。")
            rows = connection.execute(
                f"""
                SELECT id, content, read_at, note FROM records
                WHERE {' AND '.join(clauses)}
                ORDER BY read_at ASC, id ASC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
            has_more = len(rows) > page_size
            visible = [dict(row) for row in rows[:page_size]]
            next_cursor = None
            if has_more and visible:
                last = visible[-1]
                next_cursor = encode_cursor(last["read_at"], last["id"], query)
            return {
                "records": visible,
                "next_cursor": next_cursor,
                "active_count": self._active_count(connection, owner_id),
                "limit": record_limit(owner, current_time),
                "plan": plan_for(owner, current_time),
            }

    def list_all_active(self, owner_id, now=None):
        current_time = now or utc_now()
        with self._connection() as connection:
            owner = self._owner_row(connection, owner_id)
            if owner is None:
                raise ApiError(404, "account_not_found", "アカウントが見つかりません。")
            rows = connection.execute(
                """
                SELECT id, content, read_at, note FROM records
                WHERE owner_id = ? AND state = 'active'
                ORDER BY read_at ASC, id ASC
                LIMIT ?
                """,
                (owner_id, record_limit(owner, current_time) + 1),
            ).fetchall()
            return [dict(row) for row in rows]

    def update_record(self, owner_id, record_id, content, note, now=None):
        current_time = now or utc_now()
        now_text = to_iso(current_time)
        with self._transaction() as connection:
            owner = self._owner_row(connection, owner_id)
            if owner is None:
                raise ApiError(404, "account_not_found", "アカウントが見つかりません。")
            if not is_paid(owner, current_time):
                raise ApiError(403, "paid_required", "記録の編集はPaidプランで利用できます。")
            row = self._find_record(connection, owner_id, record_id)
            if row is None or row["state"] != "active":
                raise ApiError(404, "record_not_found", "記録が見つかりません。")
            connection.execute(
                """
                UPDATE records SET content = ?, note = ?, updated_at = ?
                WHERE owner_id = ? AND id = ? AND state = 'active'
                """,
                (content, note, now_text, owner_id, record_id),
            )
            return self._find_record(connection, owner_id, record_id)

    def delete_record(self, owner_id, record_id, now=None):
        current_time = now or utc_now()
        now_text = to_iso(current_time)
        with self._transaction() as connection:
            owner = self._owner_row(connection, owner_id)
            if owner is None:
                raise ApiError(404, "account_not_found", "アカウントが見つかりません。")
            row = self._find_record(connection, owner_id, record_id)
            if row is None:
                raise ApiError(404, "record_not_found", "記録が見つかりません。")
            if row["state"] == "deleted":
                return {
                    "deleted": True,
                    "already_deleted": True,
                    "active_count": self._active_count(connection, owner_id),
                }
            if row["state"] != "active":
                raise ApiError(404, "record_not_found", "記録が見つかりません。")
            connection.execute(
                """
                UPDATE records SET state = 'deleted', deleted_at = ?, updated_at = ?
                WHERE owner_id = ? AND id = ? AND state = 'active'
                """,
                (now_text, now_text, owner_id, record_id),
            )
            return {
                "deleted": True,
                "already_deleted": False,
                "active_count": self._active_count(connection, owner_id),
            }

    def transfer_guest_records(self, sid, account_id, local_records=None, now=None):
        current_time = now or utc_now()
        now_text = to_iso(current_time)
        guest_owner_id = f"guest:{sid}"
        local_records = local_records or []
        with self._transaction() as connection:
            account = self._owner_row(connection, account_id)
            if account is None or account["kind"] != "user":
                raise ApiError(401, "unauthorized", "ログイン状態を確認できません。")
            self._ensure_guest(connection, guest_owner_id, now_text)
            guest_rows = connection.execute(
                """
                SELECT * FROM records
                WHERE owner_id = ? AND (
                    state = 'active' OR transferred_to = ?
                )
                ORDER BY read_at ASC, id ASC
                """,
                (guest_owner_id, account_id),
            ).fetchall()
            pending_by_id = {}
            for item in local_records:
                record_id = item["id"]
                if self._find_record(connection, guest_owner_id, record_id) is None:
                    pending_by_id[record_id] = item
            transfer_by_id = {
                row["id"]: dict(row)
                for row in guest_rows
            }
            for record_id, item in pending_by_id.items():
                transfer_by_id[record_id] = item

            migrated_ids = []
            already_transferred = {
                row["id"]: dict(row)
                for row in guest_rows
                if row["transferred_to"] == account_id
            }
            for item in sorted(
                transfer_by_id.values(),
                key=lambda value: (value["read_at"], value["id"]),
            ):
                existing = self._find_record(connection, account_id, item["id"])
                if existing is not None:
                    if existing["state"] == "active":
                        migrated_ids.append(item["id"])
                    continue
                connection.execute(
                    """
                    INSERT INTO records(
                        owner_id, id, content, read_at, note, state, origin_paid,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)
                    """,
                    (
                        account_id,
                        item["id"],
                        item["content"],
                        item["read_at"],
                        item.get("note", ""),
                        1 if is_paid(account, current_time) else 0,
                        now_text,
                        now_text,
                    ),
                )
                migrated_ids.append(item["id"])

            logical_deleted = self._trim_to_limit(
                connection,
                account_id,
                account,
                current_time,
            )
            for record_id in already_transferred:
                existing = self._find_record(connection, account_id, record_id)
                if existing and existing["state"] == "deleted":
                    logical_deleted.append({"id": record_id, "readAt": existing["read_at"]})
                elif existing and existing["state"] == "active" and record_id not in migrated_ids:
                    migrated_ids.append(record_id)
            connection.execute(
                """
                UPDATE records SET state = 'deleted', deleted_at = ?, updated_at = ?,
                    transferred_to = ?
                WHERE owner_id = ? AND state = 'active'
                """,
                (now_text, now_text, account_id, guest_owner_id),
            )
            return {
                "migrated_ids": migrated_ids,
                "logical_deleted": logical_deleted,
                "active_count": self._active_count(connection, account_id),
            }

    def set_subscription(
        self,
        owner_id,
        event_id,
        event_created,
        status,
        paid_until,
        customer_id=None,
        subscription_id=None,
        now=None,
    ):
        current_time = now or utc_now()
        now_text = to_iso(current_time)
        normalized_status = status if status in (
            "active",
            "trialing",
            "canceled",
            "cancelled",
            "past_due",
            "unpaid",
            "incomplete",
            "incomplete_expired",
            "paused",
            "expired",
        ) else "free"
        with self._transaction() as connection:
            owner = self._owner_row(connection, owner_id)
            if owner is None or owner["kind"] != "user":
                raise ApiError(404, "account_not_found", "アカウントが見つかりません。")
            if owner.get("last_event_id") == event_id or event_created < owner.get("last_event_created", 0):
                return {
                    "ignored": True,
                    "active_count": self._active_count(connection, owner_id),
                    "logical_deleted": [],
                }
            was_paid = owner.get("subscription_status") in ("active", "trialing")
            updated_owner = dict(owner)
            updated_owner["subscription_status"] = normalized_status
            updated_owner["paid_until"] = paid_until
            will_be_paid = is_paid(updated_owner, current_time)
            logical_deleted = []

            if was_paid and not will_be_paid:
                active_rows = connection.execute(
                    """
                    SELECT id FROM records
                    WHERE owner_id = ? AND state = 'active'
                    """,
                    (owner_id,),
                ).fetchall()
                retention_until = to_iso(current_time + timedelta(days=365))
                for row in active_rows:
                    connection.execute(
                        """
                        UPDATE records
                        SET state = 'retained', retained_until = ?, updated_at = ?
                        WHERE owner_id = ? AND id = ? AND state = 'active'
                        """,
                        (retention_until, now_text, owner_id, row["id"]),
                    )

            if not was_paid and will_be_paid:
                retained = connection.execute(
                    """
                    SELECT id, read_at, origin_paid
                    FROM records
                    WHERE owner_id = ? AND state = 'retained'
                        AND retained_until > ?
                    ORDER BY read_at ASC, id ASC
                    """,
                    (owner_id, now_text),
                ).fetchall()
                active_count = self._active_count(connection, owner_id)
                overflow = max(0, active_count + len(retained) - 10000)
                if overflow:
                    paid_candidates = [row for row in retained if row["origin_paid"]]
                    fallback_candidates = [row for row in retained if not row["origin_paid"]]
                    for row in (paid_candidates + fallback_candidates)[:overflow]:
                        connection.execute(
                            """
                            UPDATE records
                            SET state = 'deleted', deleted_at = ?, updated_at = ?
                            WHERE owner_id = ? AND id = ? AND state = 'retained'
                            """,
                            (now_text, now_text, owner_id, row["id"]),
                        )
                        logical_deleted.append({"id": row["id"], "readAt": row["read_at"]})
                connection.execute(
                    """
                    UPDATE records SET state = 'active', retained_until = NULL, updated_at = ?
                    WHERE owner_id = ? AND state = 'retained' AND retained_until > ?
                    """,
                    (now_text, owner_id, now_text),
                )

            connection.execute(
                """
                UPDATE owners SET
                    subscription_status = ?,
                    paid_until = ?,
                    stripe_customer_id = COALESCE(?, stripe_customer_id),
                    subscription_id = COALESCE(?, subscription_id),
                    last_event_id = ?,
                    last_event_created = ?,
                    updated_at = ?
                WHERE owner_id = ?
                """,
                (
                    normalized_status,
                    paid_until,
                    customer_id,
                    subscription_id,
                    event_id,
                    event_created,
                    now_text,
                    owner_id,
                ),
            )
            return {
                "ignored": False,
                "active_count": self._active_count(connection, owner_id),
                "logical_deleted": logical_deleted,
            }

    def expire_paid(self, owner_id, now=None):
        current_time = now or utc_now()
        with self._connection() as connection:
            owner = self._owner_row(connection, owner_id)
        if owner is None or owner["subscription_status"] not in ("active", "trialing"):
            return
        paid_until = owner.get("paid_until")
        if not paid_until:
            return
        try:
            is_expired = datetime.fromisoformat(
                paid_until.replace("Z", "+00:00")
            ) <= current_time
        except ValueError:
            is_expired = True
        if is_expired:
            self.set_subscription(
                owner_id=owner_id,
                event_id=f"expiry:{paid_until}",
                event_created=int(current_time.timestamp()),
                status="expired",
                paid_until=paid_until,
                now=current_time,
            )

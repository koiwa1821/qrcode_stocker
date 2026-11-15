import hashlib
import hmac
import os
import uuid
from datetime import datetime, timedelta, timezone

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


class AzureTableStore:
    """Azure Table Storage adapter; account mutations use same-partition ETag batches."""

    TABLE_NAME = "QrStocker"

    def __init__(self, connection_string):
        from azure.core import MatchConditions
        from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
        from azure.data.tables import TableServiceClient, UpdateMode

        self._match_conditions = MatchConditions
        self._http_response_error = HttpResponseError
        self._not_found = ResourceNotFoundError
        self._update_mode = UpdateMode
        service = TableServiceClient.from_connection_string(connection_string)
        self.table = service.create_table_if_not_exists(self.TABLE_NAME)

    @staticmethod
    def _owner_partition(owner_id):
        return "O#" + hashlib.sha256(owner_id.encode("utf-8")).hexdigest()

    @staticmethod
    def _email_partition(email):
        return "E#" + hashlib.sha256(email.encode("utf-8")).hexdigest()

    @staticmethod
    def _customer_partition(customer_id):
        return "C#" + hashlib.sha256(customer_id.encode("utf-8")).hexdigest()

    @staticmethod
    def _session_partition(token_hash):
        return "S#" + hashlib.sha256(token_hash.encode("utf-8")).hexdigest()

    @staticmethod
    def _read_key(read_at, record_id):
        timestamp = datetime.fromisoformat(read_at.replace("Z", "+00:00")).astimezone(timezone.utc)
        return f"R#{timestamp.strftime('%Y%m%d%H%M%S%f')}#{record_id}"

    @staticmethod
    def _etag(entity):
        metadata = getattr(entity, "metadata", None)
        if isinstance(metadata, dict):
            etag = metadata.get("etag")
            if etag:
                return etag
        if metadata is not None:
            etag = getattr(metadata, "etag", None)
            if etag:
                return etag
        return entity.get("odata.etag") or entity.get("etag")

    @staticmethod
    def _entity_dict(entity):
        return dict(entity) if entity is not None else None

    def _get(self, partition_key, row_key):
        try:
            return self.table.get_entity(partition_key=partition_key, row_key=row_key)
        except self._not_found:
            return None

    def _get_owner_entity(self, owner_id):
        return self._get(self._owner_partition(owner_id), "!owner")

    @staticmethod
    def _owner_data(entity):
        if entity is None:
            return None
        owner = dict(entity)
        owner["owner_id"] = owner.get("owner_id") or owner.get("RowKey")
        owner["subscription_status"] = owner.get("subscription_status", "free")
        owner["last_event_created"] = int(owner.get("last_event_created", 0))
        owner["revision"] = int(owner.get("revision", 0))
        return owner

    @staticmethod
    def _owner_entity(owner_id, kind, email, now):
        return {
            "PartitionKey": AzureTableStore._owner_partition(owner_id),
            "RowKey": "!owner",
            "owner_id": owner_id,
            "kind": kind,
            "email": email or "",
            "subscription_status": "free",
            "paid_until": "",
            "stripe_customer_id": "",
            "subscription_id": "",
            "last_event_id": "",
            "last_event_created": 0,
            "revision": 0,
            "created_at": now,
            "updated_at": now,
        }

    @staticmethod
    def _record_data(entity):
        if entity is None:
            return None
        record = dict(entity)
        record["id"] = record.get("id") or record.get("RowKey", "").rsplit("#", 1)[-1]
        record["read_at"] = record.get("read_at", "")
        record["note"] = record.get("note", "")
        record["state"] = record.get("state", "active")
        record["origin_paid"] = bool(record.get("origin_paid", False))
        return record

    def _record_entities(self, owner_id, states=None, suffix=""):
        partition = self._owner_partition(owner_id)
        conditions = [f"PartitionKey eq '{partition}'", "RowKey ge 'R#'", "RowKey lt 'R$'"]
        if states:
            if len(states) == 1:
                conditions.append(f"state eq '{states[0]}'")
            else:
                conditions.append("(" + " or ".join(f"state eq '{state}'" for state in states) + ")")
        if suffix:
            conditions.append(suffix)
        query = self.table.query_entities(query_filter=" and ".join(conditions))
        return [self._record_data(entity) for entity in query]

    def _count_active(self, owner_id):
        return sum(
            1
            for _ in self.table.query_entities(
                query_filter=(
                    f"PartitionKey eq '{self._owner_partition(owner_id)}' "
                    "and RowKey ge 'R#' and RowKey lt 'R$' and state eq 'active'"
                ),
                select=["RowKey"],
            )
        )

    def _get_record(self, owner_id, record_id):
        partition = self._owner_partition(owner_id)
        index = self._get(partition, f"I#{record_id}")
        if index is None:
            return None
        entity = self._get(partition, index["record_row_key"])
        return self._record_data(entity)

    def _record_entity(self, owner_id, record, state, origin_paid, now, deleted_at=None, retained_until=None):
        return {
            "PartitionKey": self._owner_partition(owner_id),
            "RowKey": self._read_key(record["read_at"], record["id"]),
            "id": record["id"],
            "content": record["content"],
            "read_at": record["read_at"],
            "note": record.get("note", ""),
            "state": state,
            "origin_paid": bool(origin_paid),
            "retained_until": retained_until or "",
            "created_at": now,
            "updated_at": now,
            "deleted_at": deleted_at or "",
        }

    @staticmethod
    def _record_index(owner_id, record):
        return {
            "PartitionKey": AzureTableStore._owner_partition(owner_id),
            "RowKey": f"I#{record['id']}",
            "record_row_key": AzureTableStore._read_key(record["read_at"], record["id"]),
        }

    def _update_options(self, entity):
        etag = self._etag(entity)
        if not etag:
            raise ApiError(503, "storage_etag_missing", "保存領域の競合制御情報を取得できません。")
        return {
            "mode": self._update_mode.REPLACE,
            "etag": etag,
            "match_condition": self._match_conditions.IfNotModified,
        }

    def _submit_owner_update(self, owner_entity, operations):
        updated_owner = dict(owner_entity)
        updated_owner["revision"] = int(updated_owner.get("revision", 0)) + 1
        updated_owner["updated_at"] = to_iso(utc_now())
        actions = [
            ("update", updated_owner, self._update_options(owner_entity)),
            *operations,
        ]
        self.table.submit_transaction(actions)
        return updated_owner

    def _is_conflict(self, error):
        return isinstance(error, self._http_response_error) and error.status_code in (409, 412)

    def _ensure_owner(self, owner_id, kind="guest", email=None, now=None):
        owner = self._get_owner_entity(owner_id)
        if owner is not None:
            return owner
        now_text = to_iso(now or utc_now())
        entity = self._owner_entity(owner_id, kind, email, now_text)
        try:
            self.table.create_entity(entity=entity)
        except self._http_response_error as exc:
            if exc.status_code != 409:
                raise
        return self._get_owner_entity(owner_id)

    def ensure_guest(self, owner_id, now=None):
        self._ensure_owner(owner_id, "guest", now=now)

    def _email_index(self, email):
        return self._get(self._email_partition(email), "!owner")

    def get_owner_by_email(self, email):
        index = self._email_index(email)
        return self._owner_data(self._get_owner_entity(index["owner_id"])) if index else None

    def get_owner_by_customer(self, customer_id):
        if not customer_id:
            return None
        index = self._get(self._customer_partition(customer_id), "!owner")
        return self._owner_data(self._get_owner_entity(index["owner_id"])) if index else None

    def create_challenge(self, challenge, now=None):
        from azure.data.tables import UpdateMode

        current_time = now or utc_now()
        partition = self._email_partition(challenge["email"])
        existing = self._get(partition, "!challenge")
        if existing:
            created_at = datetime.fromisoformat(existing["created_at"].replace("Z", "+00:00"))
            if current_time - created_at < timedelta(seconds=30):
                raise ApiError(429, "verification_rate_limited", "確認コードは30秒後に再送できます。")
        entity = {
            "PartitionKey": partition,
            "RowKey": "!challenge",
            **challenge,
            "created_at": to_iso(current_time),
            "attempts": 0,
            "consumed": False,
        }
        self.table.upsert_entity(entity=entity, mode=UpdateMode.REPLACE)

    def _change_challenge(self, challenge, changes):
        updated = dict(challenge)
        updated.update(changes)
        options = self._update_options(challenge)
        try:
            self.table.update_entity(entity=updated, **options)
        except self._http_response_error as exc:
            if self._is_conflict(exc):
                raise ApiError(409, "verification_conflict", "確認コードを再取得してからやり直してください。") from exc
            raise

    def _active_device_entities(self, owner_id, now_text):
        partition = self._owner_partition(owner_id)
        return [
            entity
            for entity in self.table.query_entities(
                query_filter=(
                    f"PartitionKey eq '{partition}' and RowKey ge 'D#' and RowKey lt 'D$'"
                )
            )
            if entity.get("expires_at", "") > now_text
        ]

    def _reserve_session(self, owner_id, sid, token_hash, session_expires_at, now):
        partition = self._owner_partition(owner_id)
        now_text = to_iso(now)
        for _ in range(6):
            owner_entity = self._ensure_owner(owner_id, "user")
            owner = self._owner_data(owner_entity)
            devices = self._active_device_entities(owner_id, now_text)
            existing_device = next((item for item in devices if item.get("sid") == sid), None)
            if existing_device is None and len(devices) >= device_limit(owner, now):
                raise ApiError(409, "device_limit_reached", "認証ブラウザ数の上限に達しています。既存ブラウザをログアウトしてから再試行してください。")
            device = {
                "PartitionKey": partition,
                "RowKey": f"D#{sid}",
                "sid": sid,
                "created_at": (
                    existing_device.get("created_at", now_text)
                    if existing_device else now_text
                ),
                "last_seen_at": now_text,
                "expires_at": session_expires_at,
            }
            session = {
                "PartitionKey": partition,
                "RowKey": f"S#{token_hash}",
                "sid": sid,
                "expires_at": session_expires_at,
            }
            operations = [
                ("upsert", device, {"mode": self._update_mode.REPLACE}),
                ("upsert", session, {"mode": self._update_mode.REPLACE}),
            ]
            try:
                self._submit_owner_update(owner_entity, operations)
                from azure.data.tables import UpdateMode

                self.table.upsert_entity(
                    entity={
                        "PartitionKey": self._session_partition(token_hash),
                        "RowKey": "!session",
                        "owner_id": owner_id,
                        "sid": sid,
                        "expires_at": session_expires_at,
                    },
                    mode=UpdateMode.REPLACE,
                )
                return
            except self._http_response_error as exc:
                if not self._is_conflict(exc):
                    raise
        raise ApiError(503, "storage_conflict", "認証ブラウザ数の更新が競合しました。もう一度お試しください。")

    def verify_email(self, email, sid, code, token_hash, session_expires_at, now=None):
        now = now or utc_now()
        challenge = self._get(self._email_partition(email), "!challenge")
        if challenge is None or challenge.get("sid") != sid or challenge.get("consumed"):
            raise ApiError(400, "invalid_verification", "確認コードを再取得してください。")
        if int(challenge.get("attempts", 0)) >= 5:
            raise ApiError(429, "verification_locked", "確認コードの試行回数を超えました。再取得してください。")
        if challenge.get("expires_at", "") <= to_iso(now):
            raise ApiError(400, "verification_expired", "確認コードの有効期限が切れました。再取得してください。")
        calculated = hashlib.sha256(
            (challenge["code_salt"] + code).encode("utf-8")
        ).hexdigest()
        if not hmac.compare_digest(calculated, challenge["code_hash"]):
            self._change_challenge(
                challenge,
                {"attempts": int(challenge.get("attempts", 0)) + 1},
            )
            raise ApiError(400, "invalid_verification", "確認コードが正しくありません。")

        owner = self.get_owner_by_email(email)
        if challenge["action"] == "register":
            if owner:
                raise ApiError(409, "email_exists", "このメールアドレスは登録済みです。ログインしてください。")
            owner_id = challenge["owner_id"]
        else:
            if owner is None:
                raise ApiError(404, "account_not_found", "アカウントが見つかりません。先に登録してください。")
            owner_id = owner["owner_id"]

        challenge = self._get(self._email_partition(email), "!challenge")
        if challenge is None or challenge.get("consumed"):
            raise ApiError(409, "verification_used", "確認コードは既に使用されています。再取得してください。")
        self._change_challenge(challenge, {"consumed": True})

        if challenge["action"] == "register":
            owner_entity = self._ensure_owner(owner_id, "user", email, now)
            email_index = self._email_index(email)
            if email_index and email_index.get("owner_id") != owner_id:
                raise ApiError(409, "email_exists", "このメールアドレスは登録済みです。ログインしてください。")
            if email_index is None:
                from azure.data.tables import UpdateMode

                self.table.upsert_entity(
                    entity={
                        "PartitionKey": self._email_partition(email),
                        "RowKey": "!owner",
                        "owner_id": owner_id,
                        "email": email,
                    },
                    mode=UpdateMode.REPLACE,
                )

        self._reserve_session(owner_id, sid, token_hash, session_expires_at, now)
        consumed = self._get(self._email_partition(email), "!challenge")
        if consumed is not None:
            try:
                self.table.delete_entity(
                    partition_key=self._email_partition(email),
                    row_key="!challenge",
                    etag=self._etag(consumed),
                    match_condition=self._match_conditions.IfNotModified,
                )
            except self._http_response_error as exc:
                if not self._is_conflict(exc) and exc.status_code != 404:
                    raise
        return owner_id

    def authenticate(self, token_hash, sid, now=None):
        now_text = to_iso(now or utc_now())
        index = self._get(self._session_partition(token_hash), "!session")
        if (
            index is None
            or index.get("sid") != sid
            or index.get("expires_at", "") <= now_text
        ):
            return None
        owner_entity = self._get_owner_entity(index["owner_id"])
        owner = self._owner_data(owner_entity)
        if owner is None or owner["kind"] != "user":
            return None
        self._assert_ready(owner)
        partition = self._owner_partition(index["owner_id"])
        session = self._get(partition, f"S#{token_hash}")
        device = self._get(partition, f"D#{sid}")
        if session is None or session.get("sid") != sid:
            return None
        if device is None or device.get("expires_at", "") <= now_text:
            return None
        return owner["owner_id"]

    def logout(self, token_hash, sid):
        index = self._get(self._session_partition(token_hash), "!session")
        if index is None or index.get("sid") != sid:
            return
        owner_id = index["owner_id"]
        partition = self._owner_partition(owner_id)
        for _ in range(5):
            owner_entity = self._get_owner_entity(owner_id)
            device = self._get(partition, f"D#{sid}")
            operations = [("delete", device)] if device else []
            try:
                self._submit_owner_update(owner_entity, operations)
                break
            except self._http_response_error as exc:
                if not self._is_conflict(exc):
                    raise
        else:
            raise ApiError(503, "storage_conflict", "ログアウト処理が競合しました。もう一度お試しください。")

        sessions = list(
            self.table.query_entities(
                query_filter=(
                    f"PartitionKey eq '{partition}' and RowKey ge 'S#' "
                    f"and RowKey lt 'S$' and sid eq '{sid}'"
                )
            )
        )
        for offset in range(0, len(sessions), 100):
            batch = sessions[offset : offset + 100]
            self.table.submit_transaction([("delete", entity) for entity in batch])
        token_index = self._get(self._session_partition(token_hash), "!session")
        if token_index:
            try:
                self.table.delete_entity(
                    partition_key=self._session_partition(token_hash),
                    row_key="!session",
                    etag=self._etag(token_index),
                    match_condition=self._match_conditions.IfNotModified,
                )
            except self._http_response_error as exc:
                if not self._is_conflict(exc) and exc.status_code != 404:
                    raise

    def _assert_ready(self, owner):
        if owner.get("transition"):
            raise ApiError(503, "plan_transition", "プラン変更を処理しています。少し待ってから再試行してください。")

    def account_summary(self, owner_id, now=None):
        now = now or utc_now()
        owner_entity = self._get_owner_entity(owner_id)
        owner = self._owner_data(owner_entity)
        if owner is None:
            raise ApiError(404, "account_not_found", "アカウントが見つかりません。")
        self._assert_ready(owner)
        active_count = self._count_active(owner_id)
        device_count = len(self._active_device_entities(owner_id, to_iso(now))) if owner["kind"] == "user" else 0
        return {"owner": owner, "active_count": active_count, "device_count": device_count}

    def create_record(self, owner_id, record, now=None):
        now = now or utc_now()
        now_text = to_iso(now)
        if self._get_owner_entity(owner_id) is None:
            if owner_id.startswith("guest:"):
                self.ensure_guest(owner_id, now)
            else:
                raise ApiError(401, "unauthorized", "ログイン状態を確認できません。")
        partition = self._owner_partition(owner_id)
        for _ in range(8):
            owner_entity = self._get_owner_entity(owner_id)
            owner = self._owner_data(owner_entity)
            self._assert_ready(owner)
            existing = self._get_record(owner_id, record["id"])
            if existing:
                return {
                    "record": existing,
                    "active_count": self._count_active(owner_id),
                    "limit": record_limit(owner, now),
                    "plan": plan_for(owner, now),
                    "logical_deleted": [],
                    "duplicate": True,
                }
            active = sorted(
                self._record_entities(owner_id, ["active"]),
                key=lambda item: (item["read_at"], item["id"]),
            )
            maximum = record_limit(owner, now)
            new_is_oldest = (
                len(active) >= maximum
                and (record["read_at"], record["id"]) < (active[0]["read_at"], active[0]["id"])
            )
            new_state = "deleted" if new_is_oldest else "active"
            new_entity = self._record_entity(
                owner_id,
                record,
                new_state,
                is_paid(owner, now),
                now_text,
                deleted_at=now_text if new_is_oldest else None,
            )
            operations = [
                ("create", new_entity),
                ("create", self._record_index(owner_id, record)),
            ]
            deleted = []
            if len(active) >= maximum and not new_is_oldest:
                oldest = active[0]
                entity = self._get(partition, self._read_key(oldest["read_at"], oldest["id"]))
                if entity is None:
                    continue
                updated = dict(entity)
                updated["state"] = "deleted"
                updated["deleted_at"] = now_text
                updated["updated_at"] = now_text
                operations.append(("update", updated, self._update_options(entity)))
                deleted.append({"id": oldest["id"], "readAt": oldest["read_at"]})
            try:
                self._submit_owner_update(owner_entity, operations)
                saved_record = self._get_record(owner_id, record["id"])
                if new_is_oldest:
                    deleted.append({"id": record["id"], "readAt": record["read_at"]})
                return {
                    "record": saved_record,
                    "active_count": self._count_active(owner_id),
                    "limit": maximum,
                    "plan": plan_for(owner, now),
                    "logical_deleted": deleted,
                    "duplicate": False,
                }
            except self._http_response_error as exc:
                if not self._is_conflict(exc):
                    raise
        raise ApiError(503, "storage_conflict", "同時保存が続いているため記録を保存できませんでした。もう一度お試しください。")

    def list_records(self, owner_id, query="", cursor=None, page_size=50, now=None):
        now = now or utc_now()
        cursor_data = decode_cursor(cursor, query)
        owner = self._owner_data(self._get_owner_entity(owner_id))
        if owner is None:
            raise ApiError(404, "account_not_found", "アカウントが見つかりません。")
        self._assert_ready(owner)
        cursor_key = (
            self._read_key(cursor_data["read_at"], cursor_data["id"])
            if cursor_data else None
        )
        records = []
        has_more = False
        for entity in self.table.query_entities(
            query_filter=(
                f"PartitionKey eq '{self._owner_partition(owner_id)}' "
                "and RowKey ge 'R#' and RowKey lt 'R$' and state eq 'active'"
            )
        ):
            if cursor_key and entity["RowKey"] <= cursor_key:
                continue
            record = self._record_data(entity)
            if query and query.casefold() not in record["content"].casefold() and query.casefold() not in record["note"].casefold():
                continue
            if len(records) == page_size:
                has_more = True
                break
            records.append(record)
        next_cursor = None
        if has_more and records:
            last = records[-1]
            next_cursor = encode_cursor(last["read_at"], last["id"], query)
        return {
            "records": records,
            "next_cursor": next_cursor,
            "active_count": self._count_active(owner_id),
            "limit": record_limit(owner, now),
            "plan": plan_for(owner, now),
        }

    def list_all_active(self, owner_id, now=None):
        now = now or utc_now()
        owner = self._owner_data(self._get_owner_entity(owner_id))
        if owner is None:
            raise ApiError(404, "account_not_found", "アカウントが見つかりません。")
        self._assert_ready(owner)
        records = sorted(
            self._record_entities(owner_id, ["active"]),
            key=lambda item: (item["read_at"], item["id"]),
        )
        if len(records) > record_limit(owner, now):
            raise ApiError(503, "record_limit_invariant", "記録件数の整合性を確認できません。")
        return records

    def update_record(self, owner_id, record_id, content, note, now=None):
        now_text = to_iso(now or utc_now())
        for _ in range(6):
            owner_entity = self._get_owner_entity(owner_id)
            owner = self._owner_data(owner_entity)
            if owner is None:
                raise ApiError(404, "account_not_found", "アカウントが見つかりません。")
            self._assert_ready(owner)
            if not is_paid(owner, now):
                raise ApiError(403, "paid_required", "記録の編集はPaidプランで利用できます。")
            existing = self._get_record(owner_id, record_id)
            if existing is None or existing["state"] != "active":
                raise ApiError(404, "record_not_found", "記録が見つかりません。")
            entity = self._get(self._owner_partition(owner_id), self._read_key(existing["read_at"], record_id))
            updated = dict(entity)
            updated["content"] = content
            updated["note"] = note
            updated["updated_at"] = now_text
            try:
                self._submit_owner_update(
                    owner_entity,
                    [("update", updated, self._update_options(entity))],
                )
                return self._record_data(self._get(self._owner_partition(owner_id), entity["RowKey"]))
            except self._http_response_error as exc:
                if not self._is_conflict(exc):
                    raise
        raise ApiError(503, "storage_conflict", "同時更新が続いているため変更できませんでした。もう一度お試しください。")

    def delete_record(self, owner_id, record_id, now=None):
        now_text = to_iso(now or utc_now())
        for _ in range(6):
            owner_entity = self._get_owner_entity(owner_id)
            owner = self._owner_data(owner_entity)
            if owner is None:
                raise ApiError(404, "account_not_found", "アカウントが見つかりません。")
            self._assert_ready(owner)
            existing = self._get_record(owner_id, record_id)
            if existing is None:
                raise ApiError(404, "record_not_found", "記録が見つかりません。")
            if existing["state"] == "deleted":
                return {
                    "deleted": True,
                    "already_deleted": True,
                    "active_count": self._count_active(owner_id),
                }
            if existing["state"] != "active":
                raise ApiError(404, "record_not_found", "記録が見つかりません。")
            entity = self._get(self._owner_partition(owner_id), self._read_key(existing["read_at"], record_id))
            updated = dict(entity)
            updated["state"] = "deleted"
            updated["deleted_at"] = now_text
            updated["updated_at"] = now_text
            try:
                self._submit_owner_update(
                    owner_entity,
                    [("update", updated, self._update_options(entity))],
                )
                return {
                    "deleted": True,
                    "already_deleted": False,
                    "active_count": self._count_active(owner_id),
                }
            except self._http_response_error as exc:
                if not self._is_conflict(exc):
                    raise
        raise ApiError(503, "storage_conflict", "同時削除が続いているため変更できませんでした。もう一度お試しください。")

    def transfer_guest_records(self, sid, account_id, local_records=None, now=None):
        now = now or utc_now()
        guest_owner_id = f"guest:{sid}"
        self.ensure_guest(guest_owner_id, now)
        server_records = self._record_entities(guest_owner_id, ["active"])
        previously_transferred = self._record_entities(
            guest_owner_id,
            ["deleted"],
            f"transferred_to eq '{account_id}'",
        )
        by_id = {
            record["id"]: record
            for record in [*server_records, *previously_transferred]
        }
        for item in local_records or []:
            by_id.setdefault(item["id"], item)
        migrated_ids = []
        logical_deleted = []
        for record in sorted(by_id.values(), key=lambda item: (item["read_at"], item["id"])):
            result = self.create_record(
                account_id,
                {
                    "id": record["id"],
                    "content": record["content"],
                    "read_at": record["read_at"],
                    "note": record.get("note", ""),
                },
                now,
            )
            if result["record"]["state"] == "active":
                migrated_ids.append(record["id"])
            elif record["id"] in {item["id"] for item in previously_transferred}:
                logical_deleted.append({"id": record["id"], "readAt": record["read_at"]})
            logical_deleted.extend(result["logical_deleted"])

        if server_records:
            owner_entity = self._get_owner_entity(guest_owner_id)
            for offset in range(0, len(server_records), 99):
                actions = []
                for record in server_records[offset : offset + 99]:
                    entity = self._get(
                        self._owner_partition(guest_owner_id),
                        self._read_key(record["read_at"], record["id"]),
                    )
                    if entity is None or entity.get("state") != "active":
                        continue
                    updated = dict(entity)
                    updated.update(
                        {
                            "state": "deleted",
                            "transferred_to": account_id,
                            "deleted_at": to_iso(now),
                            "updated_at": to_iso(now),
                        }
                    )
                    actions.append(("update", updated, self._update_options(entity)))
                if actions:
                    self._submit_owner_update(owner_entity, actions)
                    owner_entity = self._get_owner_entity(guest_owner_id)
        return {
            "migrated_ids": migrated_ids,
            "logical_deleted": logical_deleted,
            "active_count": self._count_active(account_id),
        }

    def _owner_operations(self, owner_entity, operations):
        for _ in range(6):
            try:
                self._submit_owner_update(owner_entity, operations)
                return
            except self._http_response_error as exc:
                if not self._is_conflict(exc):
                    raise
                raise
        raise ApiError(503, "storage_conflict", "保存領域の更新が競合しました。もう一度お試しください。")

    def _index_customer(self, owner_id, customer_id):
        if not customer_id:
            return
        from azure.data.tables import UpdateMode

        self.table.upsert_entity(
            entity={
                "PartitionKey": self._customer_partition(customer_id),
                "RowKey": "!owner",
                "owner_id": owner_id,
            },
            mode=UpdateMode.REPLACE,
        )

    def _change_record_state(self, owner_id, record, state, now_text, retained_until=None):
        entity = self._get(
            self._owner_partition(owner_id),
            self._read_key(record["read_at"], record["id"]),
        )
        if entity is None or entity.get("state") != record.get("state"):
            return None
        updated = dict(entity)
        updated["state"] = state
        updated["updated_at"] = now_text
        updated["retained_until"] = retained_until or ""
        if state == "deleted":
            updated["deleted_at"] = now_text
        return ("update", updated, self._update_options(entity))

    def _apply_record_batches(self, owner_id, records, state, now, retained_until=None):
        changed_ids = []
        actions = []
        for record in records:
            action = self._change_record_state(
                owner_id,
                record,
                state,
                to_iso(now),
                retained_until,
            )
            if action is None:
                continue
            actions.append(action)
            if state == "deleted":
                changed_ids.append({"id": record["id"], "readAt": record["read_at"]})
            if len(actions) == 100:
                self.table.submit_transaction(actions)
                actions = []
        if actions:
            self.table.submit_transaction(actions)
        return changed_ids

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
        now = now or utc_now()
        now_text = to_iso(now)
        if status not in (
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
        ):
            status = "free"
        owner_entity = self._get_owner_entity(owner_id)
        owner = self._owner_data(owner_entity)
        if owner is None or owner["kind"] != "user":
            raise ApiError(404, "account_not_found", "アカウントが見つかりません。")
        if owner.get("last_event_id") == event_id or event_created < owner.get("last_event_created", 0):
            return {"ignored": True, "active_count": self._count_active(owner_id), "logical_deleted": []}

        target_owner = dict(owner)
        target_owner["subscription_status"] = status
        target_owner["paid_until"] = paid_until or ""
        was_paid = owner.get("subscription_status") in ("active", "trialing")
        will_be_paid = is_paid(target_owner, now)
        transition_type = None
        if was_paid and not will_be_paid:
            transition_type = "downgrade"
        elif not was_paid and will_be_paid:
            transition_type = "reactivate"

        if transition_type is None:
            for _ in range(6):
                owner_entity = self._get_owner_entity(owner_id)
                owner = self._owner_data(owner_entity)
                if owner.get("last_event_id") == event_id or event_created < owner.get("last_event_created", 0):
                    return {"ignored": True, "active_count": self._count_active(owner_id), "logical_deleted": []}
                final = dict(owner_entity)
                final.update(
                    {
                        "subscription_status": status,
                        "paid_until": paid_until or "",
                        "stripe_customer_id": customer_id or owner.get("stripe_customer_id", ""),
                        "subscription_id": subscription_id or owner.get("subscription_id", ""),
                        "last_event_id": event_id,
                        "last_event_created": int(event_created),
                        "updated_at": now_text,
                    }
                )
                try:
                    self.table.update_entity(entity=final, **self._update_options(owner_entity))
                    break
                except self._http_response_error as exc:
                    if not self._is_conflict(exc):
                        raise
            else:
                raise ApiError(503, "storage_conflict", "プラン状態の更新が競合しました。もう一度お試しください。")
            self._index_customer(
                owner_id,
                customer_id or owner.get("stripe_customer_id"),
            )
            return {
                "ignored": False,
                "active_count": self._count_active(owner_id),
                "logical_deleted": [],
            }

        existing_transition = owner.get("transition")
        if existing_transition:
            if owner.get("transition_event_id") != event_id:
                started = owner.get("transition_started_at", "")
                stale = False
                if started:
                    try:
                        stale = now - datetime.fromisoformat(started.replace("Z", "+00:00")) > timedelta(minutes=5)
                    except ValueError:
                        stale = True
                if not stale:
                    raise ApiError(503, "plan_transition", "別のプラン変更を処理しています。少し待ってから再試行してください。")
            else:
                started = owner.get("transition_started_at", "")
                if started:
                    try:
                        if now - datetime.fromisoformat(started.replace("Z", "+00:00")) <= timedelta(minutes=5):
                            raise ApiError(503, "plan_transition", "プラン変更を処理しています。Stripe webhookの再試行をお待ちください。")
                    except ValueError:
                        pass

        worker_id = str(uuid.uuid4())
        for _ in range(6):
            owner_entity = self._get_owner_entity(owner_id)
            owner = self._owner_data(owner_entity)
            claim = dict(owner_entity)
            claim.update(
                {
                    "transition": transition_type,
                    "transition_event_id": event_id,
                    "transition_worker_id": worker_id,
                    "transition_started_at": now_text,
                    "transition_status": status,
                    "transition_paid_until": paid_until or "",
                    "transition_customer_id": customer_id or owner.get("stripe_customer_id", ""),
                    "transition_subscription_id": subscription_id or owner.get("subscription_id", ""),
                    "updated_at": now_text,
                }
            )
            try:
                self.table.update_entity(entity=claim, **self._update_options(owner_entity))
                break
            except self._http_response_error as exc:
                if not self._is_conflict(exc):
                    raise
        else:
            raise ApiError(503, "storage_conflict", "プラン変更の開始が競合しました。もう一度お試しください。")

        logical_deleted = []
        try:
            if transition_type == "downgrade":
                retained_records = self._record_entities(owner_id, ["active"])
                self._apply_record_batches(
                    owner_id,
                    retained_records,
                    "retained",
                    now,
                    to_iso(now + timedelta(days=365)),
                )
            else:
                retained = [
                    record
                    for record in self._record_entities(owner_id, ["retained"])
                    if record.get("retained_until", "") > now_text
                ]
                active = self._record_entities(owner_id, ["active"])
                overflow = max(0, len(active) + len(retained) - 10000)
                if overflow:
                    candidates = sorted(
                        [record for record in retained if record["origin_paid"]],
                        key=lambda item: (item["read_at"], item["id"]),
                    )
                    if len(candidates) < overflow:
                        candidates.extend(
                            sorted(
                                [record for record in retained if not record["origin_paid"]],
                                key=lambda item: (item["read_at"], item["id"]),
                            )
                        )
                    deleted_records = candidates[:overflow]
                    logical_deleted = self._apply_record_batches(
                        owner_id,
                        deleted_records,
                        "deleted",
                        now,
                    )
                self._apply_record_batches(
                    owner_id,
                    retained,
                    "active",
                    now,
                )

            for _ in range(6):
                owner_entity = self._get_owner_entity(owner_id)
                owner_data = self._owner_data(owner_entity)
                if owner_data.get("transition_worker_id") != worker_id:
                    raise ApiError(503, "plan_transition", "プラン変更の所有権が切り替わりました。再試行してください。")
                final = dict(owner_entity)
                final.update(
                    {
                        "subscription_status": status,
                        "paid_until": paid_until or "",
                        "stripe_customer_id": customer_id or owner_data.get("stripe_customer_id", ""),
                        "subscription_id": subscription_id or owner_data.get("subscription_id", ""),
                        "last_event_id": event_id,
                        "last_event_created": int(event_created),
                        "transition": "",
                        "transition_event_id": "",
                        "transition_worker_id": "",
                        "transition_started_at": "",
                        "transition_status": "",
                        "transition_paid_until": "",
                        "transition_customer_id": "",
                        "transition_subscription_id": "",
                        "updated_at": now_text,
                    }
                )
                try:
                    self.table.update_entity(entity=final, **self._update_options(owner_entity))
                    break
                except self._http_response_error as exc:
                    if not self._is_conflict(exc):
                        raise
            else:
                raise ApiError(503, "storage_conflict", "プラン変更の完了が競合しました。再試行してください。")
        except Exception:
            raise

        self._index_customer(customer_id and owner_id, customer_id)
        return {
            "ignored": False,
            "active_count": self._count_active(owner_id),
            "logical_deleted": logical_deleted,
        }

    def expire_paid(self, owner_id, now=None):
        now = now or utc_now()
        owner = self._owner_data(self._get_owner_entity(owner_id))
        if owner is None or owner.get("subscription_status") not in ("active", "trialing"):
            return
        paid_until = owner.get("paid_until")
        if not paid_until:
            return
        try:
            expired = datetime.fromisoformat(paid_until.replace("Z", "+00:00")) <= now
        except ValueError:
            expired = True
        if expired:
            self.set_subscription(
                owner_id,
                f"expiry:{paid_until}",
                int(now.timestamp()),
                "expired",
                paid_until,
                now=now,
            )

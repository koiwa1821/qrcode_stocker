import base64
import csv
import io
import os
import tempfile
import unittest
import uuid
from datetime import timedelta
from unittest.mock import patch

from openpyxl import load_workbook

from api.domain import ApiError, to_iso, utc_now
from api.service import (
    create_checkout,
    create_record,
    export_records,
    get_session,
    process_stripe_event,
    request_verification,
    verify_email,
)
from api.storage import SQLiteStore


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = SQLiteStore(os.path.join(self.temp_dir.name, "qr.sqlite3"))
        self.now = utc_now().replace(microsecond=0)

    def tearDown(self):
        self.temp_dir.cleanup()

    def register(self, email):
        sid = str(uuid.uuid4())
        with patch.dict(os.environ, {"APP_ENV": "development"}):
            requested = request_verification(
                self.store,
                sid,
                {"email": email, "action": "register"},
                self.now,
            )
            result = verify_email(
                self.store,
                sid,
                {"email": email, "code": requested["developmentCode"]},
                self.now,
            )
        return sid, result

    def test_registration_transfers_guest_records_and_authenticates_the_browser(self):
        sid = str(uuid.uuid4())
        guest_owner = f"guest:{sid}"
        self.store.ensure_guest(guest_owner, self.now)
        guest_record_id = str(uuid.uuid4())
        read_at = self.now - timedelta(hours=2)
        self.store.create_record(
            guest_owner,
            {
                "id": guest_record_id,
                "content": "guest server record",
                "read_at": to_iso(read_at),
                "note": "",
            },
            self.now,
        )

        with patch.dict(os.environ, {"APP_ENV": "development"}):
            request = request_verification(
                self.store,
                sid,
                {"email": "transfer@example.com", "action": "register"},
                self.now,
            )
            registration = verify_email(
                self.store,
                sid,
                {"email": "transfer@example.com", "code": request["developmentCode"]},
                self.now,
            )

        account_id = registration["account"]["id"]
        self.assertIn(guest_record_id, registration["transfer"]["migrated_ids"])
        self.assertTrue(registration["authenticated"])

        offline_record_id = str(uuid.uuid4())
        create_record(
            self.store,
            account_id,
            {
                "id": offline_record_id,
                "content": "offline IndexedDB record",
                "readAt": to_iso(self.now - timedelta(minutes=30)),
                "note": "transferred after login",
            },
            self.now,
        )
        session = get_session(
            self.store,
            sid,
            registration["sessionToken"],
            self.now,
        )
        self.assertTrue(session["authenticated"])
        records = self.store.list_records(account_id, "", None, 50, self.now)["records"]
        self.assertEqual(
            {guest_record_id, offline_record_id},
            {record["id"] for record in records},
        )

    def test_production_verification_sends_email_without_returning_the_code(self):
        with (
            patch.dict(os.environ, {"APP_ENV": "production"}),
            patch("api.service._send_verification_email") as send_email,
        ):
            result = request_verification(
                self.store,
                str(uuid.uuid4()),
                {"email": "mail@example.com", "action": "register"},
                self.now,
            )

        self.assertNotIn("developmentCode", result)
        send_email.assert_called_once()

    def test_stripe_webhook_controls_paid_state_and_duplicate_events_are_ignored(self):
        sid, registration = self.register("stripe@example.com")
        owner_id = registration["account"]["id"]
        subscription_id = "sub_test_only"
        customer_id = "cus_test_only"
        active_event = {
            "id": "evt_test_active",
            "created": int(self.now.timestamp()) + 1,
            "type": "customer.subscription.updated",
            "data": {
                "object": {
                    "id": subscription_id,
                    "customer": customer_id,
                    "status": "active",
                    "current_period_end": int((self.now + timedelta(days=30)).timestamp()),
                    "metadata": {"account_id": owner_id},
                }
            },
        }
        self.assertTrue(process_stripe_event(self.store, active_event)["processed"])
        self.assertFalse(process_stripe_event(self.store, active_event)["processed"])
        self.assertEqual(
            "paid",
            get_session(self.store, sid, registration["sessionToken"], utc_now())["account"]["plan"],
        )

        canceled_event = {
            "id": "evt_test_canceled",
            "created": int(self.now.timestamp()) + 2,
            "type": "customer.subscription.deleted",
            "data": {
                "object": {
                    "id": subscription_id,
                    "customer": customer_id,
                    "status": "canceled",
                    "current_period_end": int((self.now + timedelta(days=30)).timestamp()),
                    "metadata": {"account_id": owner_id},
                }
            },
        }
        process_stripe_event(self.store, canceled_event)
        self.assertEqual(
            "canceled",
            self.store.account_summary(owner_id, utc_now())["owner"]["subscription_status"],
        )
        self.assertEqual(
            "free",
            get_session(self.store, sid, registration["sessionToken"], utc_now())["account"]["plan"],
        )

    def test_paid_exports_escape_spreadsheet_formulas_in_csv_and_xlsx(self):
        _, registration = self.register("export@example.com")
        owner_id = registration["account"]["id"]
        self.store.set_subscription(
            owner_id,
            "event_export_paid",
            int(self.now.timestamp()) + 1,
            "active",
            to_iso(self.now + timedelta(days=30)),
            now=self.now,
        )
        create_record(
            self.store,
            owner_id,
            {
                "id": str(uuid.uuid4()),
                "content": "=1+1",
                "readAt": to_iso(self.now - timedelta(minutes=1)),
                "note": "+formula",
            },
            self.now,
        )

        csv_export = export_records(self.store, owner_id, {"format": "csv"}, self.now)
        csv_text = base64.b64decode(csv_export["data"]).decode("utf-8-sig")
        csv_rows = list(csv.reader(io.StringIO(csv_text)))
        self.assertEqual("'=1+1", csv_rows[1][2])
        self.assertEqual("'+formula", csv_rows[1][3])

        xlsx_export = export_records(self.store, owner_id, {"format": "xlsx"}, self.now)
        workbook = load_workbook(
            io.BytesIO(base64.b64decode(xlsx_export["data"])),
            read_only=True,
            data_only=False,
        )
        try:
            row = next(workbook["QR記録"].iter_rows(min_row=2, values_only=True))
        finally:
            workbook.close()
        self.assertEqual("'=1+1", row[2])
        self.assertEqual("'+formula", row[3])

    def test_live_stripe_key_is_rejected_before_any_checkout_request(self):
        _, registration = self.register("checkout@example.com")
        with patch.dict(
            os.environ,
            {
                "STRIPE_SECRET_KEY": "sk_live_not-a-real-key",
                "STRIPE_PRICE_ID": "price_test_only",
                "STRIPE_SUCCESS_URL": "https://example.test/success",
                "STRIPE_CANCEL_URL": "https://example.test/cancel",
            },
        ):
            with self.assertRaises(ApiError) as context:
                create_checkout(self.store, registration["account"]["id"], self.now)
        self.assertEqual("stripe_test_key_required", context.exception.code)


if __name__ == "__main__":
    unittest.main()

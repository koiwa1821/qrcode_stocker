import unittest
from datetime import datetime, timezone

from api.table_store import AzureTableStore


class TableNotFound(Exception):
    pass


class QueryOnlyTable:
    def __init__(self):
        self.filters = []

    def get_entity(self, *, partition_key, row_key):
        if row_key == "!owner":
            return {
                "PartitionKey": partition_key,
                "RowKey": row_key,
                "owner_id": "guest:test",
                "kind": "guest",
                "subscription_status": "free",
            }
        raise TableNotFound()

    def query_entities(self, query_filter, *, select=None):
        self.filters.append(query_filter)
        return []


class AzureTableStoreTests(unittest.TestCase):
    def test_table_queries_use_azure_data_tables_query_filter_parameter(self):
        store = object.__new__(AzureTableStore)
        store.table = QueryOnlyTable()
        store._not_found = TableNotFound
        now = datetime.now(timezone.utc)

        self.assertEqual([], store._record_entities("guest:test"))
        self.assertEqual(0, store._count_active("guest:test"))
        self.assertEqual([], store._active_device_entities("guest:test", now.isoformat()))
        listed = store.list_records("guest:test", "", None, 50, now)

        self.assertEqual([], listed["records"])
        self.assertEqual(5, len(store.table.filters))
        self.assertTrue(all(query for query in store.table.filters))


if __name__ == "__main__":
    unittest.main()

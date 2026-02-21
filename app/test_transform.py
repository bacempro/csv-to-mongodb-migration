import unittest
import tempfile
from datetime import datetime
import pandas as pd

import migrate_to_mongo as m


class TestTransformHelpers(unittest.TestCase):
    def test_normalize_lower(self):
        self.assertEqual(m.normalize_lower("  John Doe  "), "john doe")
        self.assertIsNone(m.normalize_lower(""))
        self.assertIsNone(m.normalize_lower(None))

    def test_coerce_int(self):
        self.assertEqual(m.coerce_int("42"), 42)
        self.assertEqual(m.coerce_int(42.0), 42)
        self.assertIsNone(m.coerce_int(""))
        self.assertIsNone(m.coerce_int("not-an-int"))

    def test_coerce_float(self):
        self.assertAlmostEqual(m.coerce_float("1234.5"), 1234.5)
        self.assertAlmostEqual(m.coerce_float("1,234.50"), 1234.5)
        self.assertIsNone(m.coerce_float(""))
        self.assertIsNone(m.coerce_float("not-a-number"))

    def test_coerce_date_dayfirst_and_date_only(self):
        dt = m.coerce_date("20/08/2019")  # dayfirst=True
        self.assertIsInstance(dt, datetime)
        self.assertEqual(dt.year, 2019)
        self.assertEqual(dt.month, 8)
        self.assertEqual(dt.day, 20)
        # normalized to date-only
        self.assertEqual((dt.hour, dt.minute, dt.second, dt.microsecond), (0, 0, 0, 0))


class TestTransformRow(unittest.TestCase):
    def test_transform_row_happy_path(self):
        row = pd.Series({
            "Name": "Alice Smith",
            "Age": "30",
            "Gender": "Female",
            "Blood Type": "A+",
            "Medical Condition": "Diabetes",
            "Date of Admission": "01/02/2020",
            "Doctor": "Dr Who",
            "Hospital": "General Hospital",
            "Insurance Provider": "ACME",
            "Billing Amount": "1000.50",
            "Room Number": "012",
            "Admission Type": "Emergency",
            "Discharge Date": "05/02/2020",
            "Medication": "Metformin",
            "Test Results": "Normal",
        })
        doc = m.transform_row(row)
        self.assertIsNotNone(doc)

        # natural key fields normalized
        self.assertEqual(doc["name"], "alice smith")
        self.assertEqual(doc["gender"], "female")
        self.assertEqual(doc["blood_type"], "a+")
        self.assertEqual(doc["hospital"], "general hospital")

        # date parsing
        self.assertEqual(doc["date_of_admission"], datetime(2020, 2, 1))
        self.assertEqual(doc["discharge_date"], datetime(2020, 2, 5))

        # typing
        self.assertEqual(doc["age"], 30)
        self.assertAlmostEqual(doc["billing_amount"], 1000.50)
        self.assertEqual(doc["room_number"], "012")  # kept as string

        # metadata exists
        self.assertIn("ingested_at", doc)
        self.assertIn("last_modified_at", doc)

    def test_transform_row_missing_key_returns_none(self):
        # Missing Hospital => incomplete natural key
        row = pd.Series({
            "Name": "Bob",
            "Age": "40",
            "Gender": "Male",
            "Blood Type": "O+",
            "Medical Condition": "Asthma",
            "Date of Admission": "01/01/2020",
            "Doctor": "Dr X",
            "Hospital": "",  # missing
            "Insurance Provider": "ACME",
            "Billing Amount": "10",
            "Room Number": "1",
            "Admission Type": "Urgent",
            "Discharge Date": "02/01/2020",
            "Medication": "X",
            "Test Results": "Y",
        })
        self.assertIsNone(m.transform_row(row))


class TestIterDocuments(unittest.TestCase):
    def test_iter_documents_skips_duplicate_keys_in_same_csv(self):
        # Create a tiny CSV with two identical admissions (same natural key)
        rows = [
            {
                "Name": "Jane Doe",
                "Age": 25,
                "Gender": "Female",
                "Blood Type": "B-",
                "Medical Condition": "Flu",
                "Date of Admission": "10/01/2021",
                "Doctor": "Dr A",
                "Hospital": "City Hospital",
                "Insurance Provider": "InsureCo",
                "Billing Amount": 100,
                "Room Number": "10",
                "Admission Type": "Routine",
                "Discharge Date": "11/01/2021",
                "Medication": "Med",
                "Test Results": "OK",
            },
            {
                # duplicate natural key (even if age differs, your key ignores age)
                "Name": "Jane Doe",
                "Age": 26,
                "Gender": "Female",
                "Blood Type": "B-",
                "Medical Condition": "Flu",
                "Date of Admission": "10/01/2021",
                "Doctor": "Dr A",
                "Hospital": "City Hospital",
                "Insurance Provider": "InsureCo",
                "Billing Amount": 100,
                "Room Number": "10",
                "Admission Type": "Routine",
                "Discharge Date": "11/01/2021",
                "Medication": "Med",
                "Test Results": "OK",
            },
        ]
        df = pd.DataFrame(rows)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
            df.to_csv(f.name, index=False)
            csv_path = f.name

        stats = {"total_rows": 0, "missing_key_rows": 0, "duplicate_key_rows": 0, "seen_keys": set()}
        batches = list(m.iter_documents(csv_path, chunksize=10, stats=stats))

        # Only 1 doc should survive in the yielded batches
        yielded_docs = [d for batch in batches for d in batch]
        self.assertEqual(len(yielded_docs), 1)

        # Stats should reflect 2 total rows, 1 duplicate
        self.assertEqual(stats["total_rows"], 2)
        self.assertEqual(stats["duplicate_key_rows"], 1)
        self.assertEqual(stats["missing_key_rows"], 0)


if __name__ == "__main__":
    unittest.main()


#!/usr/bin/env python3
"""
MongoDB CSV Migration Script (Healthcare dataset) — Upsert & Unique Index
-------------------------------------------------------------------------
Adds:
  - Case-normalization for *all* natural key fields (name, gender, blood_type, hospital).
  - Date normalization to date-only (00:00:00) for admission/discharge.
  - Unique compound index on (name, gender, blood_type, date_of_admission, hospital).
  - Idempotent bulk upserts (no duplicates on reruns).
  - Optional .env support.

Natural key : (name, gender, blood_type, date_of_admission, hospital)

Usage (examples):
  # One-time: create index and upsert
  python migrate_to_mongo.py --csv /path/healthcare_dataset.csv --create-indexes --upsert

  # Preview only
  python migrate_to_mongo.py --csv /path/healthcare_dataset.csv --dry-run

  # Plain insert (no upsert)
  python migrate_to_mongo.py --csv /path/healthcare_dataset.csv --no-upsert

  # Print requirements
  python migrate_to_mongo.py --print-requirements

"""
import argparse
import os
import sys
import logging
from typing import Dict, Iterable, List, Optional, Tuple
from datetime import datetime

# Optional .env support (won't fail if not installed)
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass

import pandas as pd
from pymongo import MongoClient, UpdateOne
from pymongo.errors import BulkWriteError

__REQUIREMENTS__ = [
    "pandas>=2.0,<3",
    "pymongo>=4.6,<5",
    "python-dotenv>=1.0,<2",
]

EXPECTED_COLUMNS = [
    "Name",
    "Age",
    "Gender",
    "Blood Type",
    "Medical Condition",
    "Date of Admission",
    "Doctor",
    "Hospital",
    "Insurance Provider",
    "Billing Amount",
    "Room Number",
    "Admission Type",
    "Discharge Date",
    "Medication",
    "Test Results",
]

NATURAL_KEY_FIELDS = [  # normalized field names used in Mongo
    "name",
    "gender",
    "blood_type",
    "date_of_admission",
    "hospital",
]

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Migrate healthcare CSV into MongoDB with idempotent upserts.")
    parser.add_argument("--csv", dest="csv_path", required=False,
                        default=os.environ.get("CSV_PATH"),
                        help="Path to the input CSV file (or env CSV_PATH).")
    parser.add_argument("--mongo-uri", dest="mongo_uri", required=False,
                        default=os.environ.get("MONGO_URI", "mongodb://localhost:27017"),
                        help="Mongo connection string (or env MONGO_URI).")
    parser.add_argument("--db", dest="db_name", required=False,
                        default=os.environ.get("MONGO_DB", "healthcare"),
                        help="Mongo database name (or env MONGO_DB).")
    parser.add_argument("--collection", dest="collection_name", required=False,
                        default=os.environ.get("MONGO_COLLECTION", "patients"),
                        help="Mongo collection name (or env MONGO_COLLECTION).")
    parser.add_argument("--batch-size", dest="batch_size", type=int, default=1000,
                        help="Batch size for bulk operations.")
    parser.add_argument("--chunksize", dest="chunksize", type=int, default=5000,
                        help="Read the CSV in chunks of this many rows (streaming).")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true",
                        help="Validate and transform, but do not write to MongoDB.")
    parser.add_argument("--print-requirements", action="store_true",
                        help="Print pip requirements and exit.")
    parser.add_argument("--create-indexes", dest="create_indexes", action="store_true",
                        help="Create unique compound index for idempotent upserts.")
    # Upsert flags (default True)
    upsert_group = parser.add_mutually_exclusive_group()
    upsert_group.add_argument("--upsert", dest="upsert", action="store_true", default=True,
                              help="Use upsert mode (default).")
    upsert_group.add_argument("--no-upsert", dest="upsert", action="store_false",
                              help="Disable upsert; perform plain inserts.")
    parser.add_argument("--report-path",dest="report_path",default=os.environ.get("REPORT_PATH"),
                        help="Path for report.txt",)
    parser.add_argument("--log-level", dest="log_level", default=os.environ.get("LOG_LEVEL","INFO"),
                        help="Logging level (DEBUG, INFO, WARNING, ERROR).")
    return parser.parse_args()

def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

def validate_columns(columns: List[str]) -> None:
    missing = [c for c in EXPECTED_COLUMNS if c not in columns]
    extra = [c for c in columns if c not in EXPECTED_COLUMNS]
    if missing:
        raise ValueError(f"Missing expected columns: {missing}")
    if extra:
        logging.warning("Extra columns present and will be ignored: %s", extra)

def coerce_string(val) -> Optional[str]:
    if pd.isna(val):
        return None
    s = str(val).strip()
    return s if s != "" else None

def normalize_lower(val) -> Optional[str]:
    s = coerce_string(val)
    return s.lower() if s else None

def coerce_int(val) -> Optional[int]:
    if pd.isna(val) or val == "":
        return None
    try:
        return int(float(val))
    except Exception:
        return None

def coerce_float(val) -> Optional[float]:
    if pd.isna(val) or val == "":
        return None
    try:
        if isinstance(val, str):
            val = val.replace(",", "")
        return float(val)
    except Exception:
        return None

def coerce_date(val) -> Optional[datetime]:
    """Parse to datetime and normalize to date-only (00:00:00)."""
    if pd.isna(val) or val == "":
        return None
    dt = pd.to_datetime(val, dayfirst=True, errors="coerce")
    if pd.isna(dt):
        return None
    py = dt.to_pydatetime().replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    return py

def transform_row(row: pd.Series) -> Optional[Dict]:
    """Map CSV row to MongoDB document with normalized types/case.
       Returns None if the natural-key fields are missing (skips row)."""
    name = normalize_lower(row.get("Name"))
    gender = normalize_lower(row.get("Gender"))
    blood_type = normalize_lower(row.get("Blood Type"))
    hospital = normalize_lower(row.get("Hospital"))
    doa = coerce_date(row.get("Date of Admission"))

    # If any natural key field is missing, skip to avoid index errors
    if not all([name, gender, blood_type, hospital, doa]):
        logging.warning("Skipping row with incomplete natural key: %s", {
            "name": name, "gender": gender, "blood_type": blood_type, "date_of_admission": doa, "hospital": hospital
        })
        return None

    now = datetime.utcnow()
    doc = {
        "name": name,
        "age": coerce_int(row.get("Age")),
        "gender": gender,
        "blood_type": blood_type,
        "medical_condition": coerce_string(row.get("Medical Condition")),
        "date_of_admission": doa,
        "doctor": coerce_string(row.get("Doctor")),
        "hospital": hospital,
        "insurance_provider": coerce_string(row.get("Insurance Provider")),
        "billing_amount": coerce_float(row.get("Billing Amount")),
        "room_number": coerce_string(row.get("Room Number")),
        "admission_type": coerce_string(row.get("Admission Type")),
        "discharge_date": coerce_date(row.get("Discharge Date")),
        "medication": coerce_string(row.get("Medication")),
        "test_results": coerce_string(row.get("Test Results")),
        # Operational metadata
        "ingested_at": now,
        "last_modified_at": now,
        "source": "csv_migration_v2",
    }
    return doc

def natural_key_tuple(doc: Dict) -> Tuple:
    return (doc["name"], doc["gender"], doc["blood_type"], doc["date_of_admission"], doc["hospital"])

def iter_documents(csv_path: str, chunksize: int, stats: Dict) -> Iterable[List[Dict]]:
    first = True
    for chunk in pd.read_csv(csv_path, chunksize=chunksize):
        if first:
            validate_columns(list(chunk.columns))
            first = False
        docs = []
        for _, row in chunk.iterrows():
            stats["total_rows"] += 1
            d = transform_row(row)
            if d is None:
                stats["missing_key_rows"] += 1
                continue
            key = natural_key_tuple(d)
            if key in stats["seen_keys"]:
                stats["duplicate_key_rows"] += 1
                # Skip duplicates to avoid redundant upserts in this run
                continue
            stats["seen_keys"].add(key)
            docs.append(d)
        if docs:
            yield docs

def get_collection(mongo_uri: str, db_name: str, collection_name: str):
    client = MongoClient(mongo_uri)
    db = client[db_name]
    return db[collection_name]

def ensure_unique_index(collection) -> None:
    """Create unique compound index on the natural key."""
    idx_spec = [
        ("name", 1),
        ("gender", 1),
        ("blood_type", 1),
        ("date_of_admission", 1),
        ("hospital", 1),
    ]
    try:
        name = collection.create_index(idx_spec, unique=True, name="uniq_admission")
        logging.info("Ensured unique index: %s", name)
    except Exception as e:
        logging.error("Failed to create index: %s", e)
        raise

def bulk_write(collection, docs: List[Dict], upsert: bool) -> int:
    if not docs:
        return 0
    if not upsert:
        try:
            res = collection.insert_many(docs, ordered=False)
            return len(res.inserted_ids)
        except BulkWriteError as bwe:
            logging.error("Bulk write error (insert): %s", bwe.details)
            return len(bwe.details.get("writeErrors", []))

    ops = []
    for d in docs:
        filt = {k: d[k] for k in ["name", "gender", "blood_type", "date_of_admission", "hospital"]}
        set_fields = d.copy()
        set_fields.pop("ingested_at", None)
        set_fields["last_modified_at"] = datetime.utcnow()
        ops.append(UpdateOne(filt, {"$set": set_fields, "$setOnInsert": {"ingested_at": d.get("ingested_at")}}, upsert=True))
    try:
        res = collection.bulk_write(ops, ordered=False)
        return (res.upserted_count or 0) + (res.modified_count or 0)
    except BulkWriteError as bwe:
        logging.error("Bulk write error (upsert): %s", bwe.details)
        return 0

def insert_or_upsert(collection, docs_iter: Iterable[List[Dict]], batch_size: int, upsert: bool) -> int:
    total = 0
    buffer: List[Dict] = []
    for docs in docs_iter:
        for d in docs:
            buffer.append(d)
            if len(buffer) >= batch_size:
                total += bulk_write(collection, buffer, upsert=upsert)
                buffer.clear()
    if buffer:
        total += bulk_write(collection, buffer, upsert=upsert)
    return total

def append_report(report_path: str, csv_path: str, stats: Dict, written: int) -> None:
    ts = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    line = (
        f"[{ts}] csv={os.path.basename(csv_path)} "
        f"total_rows={stats['total_rows']} "
        f"duplicates_in_csv={stats['duplicate_key_rows']} "
        f"missing_key_rows={stats['missing_key_rows']} "
        f"upserted_or_modified={written}\n"
    )
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, "a", encoding="utf-8") as f:
        f.write(line)
    logging.info("Appended report entry to %s", report_path)

def main():
    args = parse_args()
    setup_logging(args.log_level)

    if args.print_requirements:
        print("\n".join(__REQUIREMENTS__))
        sys.exit(0)

    if not args.csv_path:
        logging.error("--csv or CSV_PATH env is required.")
        sys.exit(2)

    logging.info("Reading CSV from: %s", args.csv_path)

    stats = {
        "total_rows": 0,
        "missing_key_rows": 0,
        "duplicate_key_rows": 0,
        "seen_keys": set(),  # Set[Tuple]
    }

    docs_iter = iter_documents(args.csv_path, chunksize=args.chunksize, stats=stats)

    if args.dry_run:
        try:
            first_batch = next(docs_iter)
        except StopIteration:
            logging.warning("CSV appears empty; nothing to preview.")
            sys.exit(0)
        preview = first_batch[:5]
        import json
        print(json.dumps(preview, default=str, indent=2))
        sys.exit(0)

    collection = get_collection(args.mongo_uri, args.db_name, args.collection_name)

    if args.create_indexes:
        ensure_unique_index(collection)

    logging.info("Mode: %s", "UPSERT" if args.upsert else "INSERT")
    written = insert_or_upsert(collection, docs_iter, batch_size=args.batch_size, upsert=args.upsert)
    append_report(args.report_path, args.csv_path, stats, written)
    logging.info("Written %d documents into %s.%s", written, args.db_name, args.collection_name)

if __name__ == "__main__":
    main()

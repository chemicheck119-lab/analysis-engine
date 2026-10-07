"""Read-only hash comparison against a private pre-migration object inventory."""

import argparse
import hashlib
import json
import os
from pathlib import Path

import pyarrow.fs as fs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    args = parser.parse_args()
    endpoint = os.getenv("CHEMICHECK_ICEBERG_S3_ENDPOINT", "")
    if endpoint != "http://127.0.0.1:59000":
        raise RuntimeError("LOCAL_OBJECT_ENDPOINT_REQUIRED")
    s3 = fs.S3FileSystem(
        endpoint_override="127.0.0.1:59000",
        scheme="http",
        region="us-east-1",
        access_key=os.environ["CHEMICHECK_ICEBERG_S3_KEY"],
        secret_key=os.environ["CHEMICHECK_ICEBERG_S3_SECRET"],
    )
    rows = json.loads(args.inventory.read_text())
    for row in rows:
        data = s3.open_input_file(row["path"]).read()
        if (
            len(data) != row["bytes"]
            or hashlib.sha256(data).hexdigest() != row["sha256"]
        ):
            raise RuntimeError("OBJECT_PRESERVATION_MISMATCH")
    print(json.dumps({"matched_objects": len(rows), "status": "MATCHED"}))


if __name__ == "__main__":
    main()

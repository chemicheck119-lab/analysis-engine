"""Create only the disposable local S3 fixture bucket, never an AWS resource."""

import pyarrow.fs as fs

if __name__ == "__main__":
    fixture = fs.S3FileSystem(
        access_key="localfixture",
        secret_key="localfixtureonly",
        endpoint_override="127.0.0.1:59000",
        scheme="http",
        region="us-east-1",
        allow_bucket_creation=True,
    )
    fixture.create_dir("reference")
    print("Local fixture bucket ready")

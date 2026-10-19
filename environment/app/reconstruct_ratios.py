#!/usr/bin/env python3
import hashlib
import json
import math
import os
import sys
import tempfile
from fractions import Fraction
from pathlib import Path


def read_batch(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def interval_for(display, digits):
    value = float(display)
    exponent = math.floor(math.log10(abs(value)))
    step = 10 ** (exponent - digits + 1)
    return value - step / 2, value + step / 2


def reduced(p, q):
    common = math.gcd(abs(p), q)
    return p // common, q // common


def reconstruct(sample):
    lo, hi = interval_for(sample["display"], sample["significant_digits"])
    limit = sample["denominator_limit"]
    for q in range(1, limit + 1):
        p = round((lo + hi) / 2 * q)
        value = p / q
        if lo <= value <= hi:
            p, q = reduced(p, q)
            return "used", f"{p}/{q}", q
    return "none", None, None


def digest(value):
    raw = json.JSONEncoder(
        ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode(value)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def ledger_state(path):
    if not os.path.exists(path):
        return [], "0" * 64
    with open(path, "rb") as handle:
        raw = handle.read()
    lines = raw.splitlines(keepends=True)
    records = [json.loads(line) for line in lines]
    tail = hashlib.sha256(raw.splitlines(keepends=True)[-1]).hexdigest() if lines else "0" * 64
    return records, tail


def publish(certificate_path, ledger_path, certificate, ledger_bytes):
    cert_dir = os.path.dirname(os.path.abspath(certificate_path)) or "."
    ledger_dir = os.path.dirname(os.path.abspath(ledger_path)) or "."
    os.makedirs(cert_dir, exist_ok=True)
    os.makedirs(ledger_dir, exist_ok=True)
    fd, cert_tmp = tempfile.mkstemp(prefix=".ratio-", dir=cert_dir)
    os.close(fd)
    Path(cert_tmp).write_bytes(certificate)
    ledger_tmp = None
    if ledger_dir == cert_dir:
        ledger_tmp = cert_tmp + ".ledger"
    else:
        fd, ledger_tmp = tempfile.mkstemp(prefix=".ratio-", dir=ledger_dir)
        os.close(fd)
    Path(ledger_tmp).write_bytes(ledger_bytes)
    os.replace(cert_tmp, certificate_path)
    os.replace(ledger_tmp, ledger_path)


def main(argv):
    if len(argv) != 4:
        return 2
    batch = read_batch(argv[1])
    records, tail = ledger_state(argv[3])
    existing = {(row["batch_id"], row["sample_id"]): row for row in records}
    new_lines = []
    output_samples = []
    counts = {"used": 0, "ambiguous": 0, "none": 0}
    for sample in batch["samples"]:
        status, fraction, denominator = reconstruct(sample)
        counts[status] += 1
        item = {
            "sample_id": sample["sample_id"],
            "status": status,
            "interval": {"lo": "0", "hi": "0", "include_lo": True, "include_hi": True},
        }
        if status == "used":
            item["fraction"] = fraction
            item["denominator"] = denominator
        output_samples.append(item)
        key = (batch["batch_id"], sample["sample_id"])
        if key in existing:
            continue
        record = {
            "batch_id": batch["batch_id"],
            "sample_id": sample["sample_id"],
            "measurement_sha256": digest(sample),
            "status": status,
            "fraction": fraction,
            "prev_sha256": tail,
        }
        line = json.JSONEncoder(ensure_ascii=False, separators=(",", ":")).encode(record) + "\n"
        new_lines.append(line)
        tail = hashlib.sha256(line.encode("utf-8")).hexdigest()
    report = {
        "batch_id": batch["batch_id"],
        "samples": output_samples,
        "summary": {
            "total": len(batch["samples"]),
            "used": counts["used"],
            "ambiguous": counts["ambiguous"],
            "none": counts["none"],
            "ledger_appended": len(new_lines),
        },
    }
    certificate = (
        json.JSONEncoder(ensure_ascii=False, separators=(",", ":")).encode(report) + "\n"
    ).encode("utf-8")
    if os.path.exists(argv[3]):
        with open(argv[3], "rb") as handle:
            old_ledger = handle.read()
    else:
        old_ledger = b""
    publish(argv[2], argv[3], certificate, old_ledger + "".join(new_lines).encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

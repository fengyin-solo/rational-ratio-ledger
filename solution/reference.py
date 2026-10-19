#!/usr/bin/env python3
import hashlib
import json
import math
import os
import re
import shutil
import sys
import tempfile
import traceback
from fractions import Fraction


ZERO_DIGEST = "0" * 64
HEX64 = re.compile(r"^[0-9a-f]{64}$")
DISPLAY = re.compile(r"^-?\d+(?:\.\d+)?$")
TOP_KEYS = {"batch_id", "samples"}
SAMPLE_KEYS = {
    "sample_id",
    "display",
    "significant_digits",
    "denominator_limit",
}
LEDGER_KEYS = {
    "batch_id",
    "sample_id",
    "measurement_sha256",
    "status",
    "fraction",
    "prev_sha256",
}


class ReviewError(Exception):
    pass


def no_duplicate_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ReviewError("duplicate key")
        result[key] = value
    return result


def load_strict(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle, object_pairs_hook=no_duplicate_object)


def is_plain_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def fraction_text(value):
    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"


def parse_fraction_text(text):
    if not isinstance(text, str) or not text:
        raise ReviewError("bad fraction")
    if "/" in text:
        pieces = text.split("/")
        if len(pieces) != 2 or not pieces[0] or not pieces[1]:
            raise ReviewError("bad fraction")
        numerator = int(pieces[0])
        denominator = int(pieces[1])
    else:
        numerator = int(text)
        denominator = 1
    if denominator <= 0:
        raise ReviewError("bad denominator")
    value = Fraction(numerator, denominator)
    if fraction_text(value) != text:
        raise ReviewError("fraction not reduced")
    return value


def decimal_exponent(display):
    unsigned = display[1:] if display.startswith("-") else display
    if "." in unsigned:
        whole, fraction = unsigned.split(".")
    else:
        whole, fraction = unsigned, ""
    digits = whole + fraction
    first = 0
    while digits[first] == "0":
        first += 1
    return len(whole) - 1 - first


def validate_sample(sample):
    if not isinstance(sample, dict) or set(sample) != SAMPLE_KEYS:
        raise ReviewError("bad sample keys")
    sample_id = sample["sample_id"]
    if not isinstance(sample_id, str) or not sample_id:
        raise ReviewError("bad sample id")
    display = sample["display"]
    if not isinstance(display, str) or not DISPLAY.fullmatch(display):
        raise ReviewError("bad display")
    if Fraction(display) == 0:
        raise ReviewError("zero display")
    if abs(Fraction(display)) > 1000:
        raise ReviewError("display magnitude")
    digits = sample["significant_digits"]
    if not is_plain_int(digits) or not 1 <= digits <= 15:
        raise ReviewError("bad significant digits")
    limit = sample["denominator_limit"]
    if not is_plain_int(limit) or not 1 <= limit <= 100000:
        raise ReviewError("bad denominator limit")


def read_batch(path):
    batch = load_strict(path)
    if not isinstance(batch, dict) or set(batch) != TOP_KEYS:
        raise ReviewError("bad batch keys")
    batch_id = batch["batch_id"]
    if not isinstance(batch_id, str) or not batch_id or batch_id != batch_id.strip():
        raise ReviewError("bad batch id")
    samples = batch["samples"]
    if not isinstance(samples, list) or not samples:
        raise ReviewError("empty samples")
    seen = set()
    for sample in samples:
        validate_sample(sample)
        if sample["sample_id"] in seen:
            raise ReviewError("duplicate sample id")
        seen.add(sample["sample_id"])
    return batch


def measurement_digest(sample):
    ordered = {
        "denominator_limit": sample["denominator_limit"],
        "display": sample["display"],
        "sample_id": sample["sample_id"],
        "significant_digits": sample["significant_digits"],
    }
    raw = json.dumps(ordered, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def reading_interval(sample):
    display = sample["display"]
    value = Fraction(display)
    exponent = decimal_exponent(display)
    step = Fraction(10) ** (exponent - sample["significant_digits"] + 1)
    lo = value - step / 2
    hi = value + step / 2
    return lo, hi, value > 0


def reconstruct(sample):
    lo, hi, positive = reading_interval(sample)
    include_lo = positive
    include_hi = not positive
    for denominator in range(1, sample["denominator_limit"] + 1):
        scaled_lo = lo * denominator
        scaled_hi = hi * denominator
        first = math.ceil(scaled_lo)
        if not include_lo and scaled_lo.denominator == 1:
            first += 1
        last = math.floor(scaled_hi)
        if not include_hi and scaled_hi.denominator == 1:
            last -= 1
        candidates = [
            Fraction(numerator, denominator)
            for numerator in range(first, last + 1)
            if math.gcd(abs(numerator), denominator) == 1
        ]
        if not candidates:
            continue
        if len(candidates) == 1:
            status = "used"
        else:
            status = "ambiguous"
        return {
            "status": status,
            "lo": lo,
            "hi": hi,
            "include_lo": include_lo,
            "include_hi": include_hi,
            "denominator": denominator,
            "candidates": sorted(candidates),
        }
    return {
        "status": "none",
        "lo": lo,
        "hi": hi,
        "include_lo": include_lo,
        "include_hi": include_hi,
    }


def certificate_entry(sample, result):
    entry = {
        "sample_id": sample["sample_id"],
        "status": result["status"],
        "interval": {
            "lo": fraction_text(result["lo"]),
            "hi": fraction_text(result["hi"]),
            "include_lo": result["include_lo"],
            "include_hi": result["include_hi"],
        },
    }
    if result["status"] == "used":
        value = result["candidates"][0]
        entry["fraction"] = fraction_text(value)
        entry["denominator"] = value.denominator
    elif result["status"] == "ambiguous":
        entry["denominator"] = result["denominator"]
        entry["candidates"] = [fraction_text(value) for value in result["candidates"]]
    return entry


def read_ledger(path):
    if not os.path.exists(path):
        return [], [], ZERO_DIGEST
    if not os.path.isfile(path):
        raise ReviewError("ledger is not a regular file")
    with open(path, "rb") as handle:
        raw = handle.read()
    if not raw:
        return [], [], ZERO_DIGEST
    if not raw.endswith(b"\n"):
        raise ReviewError("ledger lacks final newline")
    physical = raw.splitlines(keepends=True)
    records = []
    keys = set()
    expected_previous = ZERO_DIGEST
    for line in physical:
        if not line or line == b"\n":
            raise ReviewError("empty ledger line")
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ReviewError("ledger decode") from exc
        try:
            record = json.loads(text, object_pairs_hook=no_duplicate_object)
        except (json.JSONDecodeError, ReviewError) as exc:
            raise ReviewError("bad ledger json") from exc
        if not isinstance(record, dict) or set(record) != LEDGER_KEYS:
            raise ReviewError("bad ledger keys")
        if not isinstance(record["batch_id"], str) or not record["batch_id"]:
            raise ReviewError("bad ledger batch")
        if not isinstance(record["sample_id"], str) or not record["sample_id"]:
            raise ReviewError("bad ledger sample")
        if not isinstance(record["measurement_sha256"], str) or not HEX64.fullmatch(
            record["measurement_sha256"]
        ):
            raise ReviewError("bad measurement digest")
        if record["status"] not in {"used", "ambiguous", "none"}:
            raise ReviewError("bad ledger status")
        if record["status"] == "used":
            parse_fraction_text(record["fraction"])
        elif record["fraction"] is not None:
            raise ReviewError("unexpected ledger fraction")
        if not isinstance(record["prev_sha256"], str) or not HEX64.fullmatch(
            record["prev_sha256"]
        ):
            raise ReviewError("bad previous digest")
        if record["prev_sha256"] != expected_previous:
            raise ReviewError("broken ledger chain")
        key = (record["batch_id"], record["sample_id"])
        if key in keys:
            raise ReviewError("duplicate ledger key")
        keys.add(key)
        records.append(record)
        expected_previous = hashlib.sha256(line).hexdigest()
    return records, physical, expected_previous


def file_state(path):
    if not os.path.exists(path):
        return None
    if not os.path.isfile(path):
        raise ReviewError("output target is not a regular file")
    with open(path, "rb") as handle:
        return handle.read(), os.stat(path).st_mode & 0o777


def write_temp(directory, data, mode):
    fd, path = tempfile.mkstemp(prefix=".ratio-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(path, mode)
    except Exception:
        if os.path.exists(path):
            os.unlink(path)
        raise
    return path


def restore(path, state):
    if state is None:
        if os.path.exists(path):
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.unlink(path)
        return
    data, mode = state
    directory = os.path.dirname(os.path.abspath(path))
    replacement = write_temp(directory, data, mode)
    os.replace(replacement, path)


def publish(certificate_path, ledger_path, certificate_bytes, ledger_bytes):
    certificate_abs = os.path.abspath(certificate_path)
    ledger_abs = os.path.abspath(ledger_path)
    if certificate_abs == ledger_abs:
        raise ReviewError("output paths coincide")
    certificate_dir = os.path.dirname(certificate_abs)
    ledger_dir = os.path.dirname(ledger_abs)
    if not os.path.isdir(certificate_dir) or not os.path.isdir(ledger_dir):
        raise ReviewError("output parent missing")
    certificate_state = file_state(certificate_abs)
    ledger_state = file_state(ledger_abs)
    certificate_tmp = None
    ledger_tmp = None
    try:
        certificate_tmp = write_temp(certificate_dir, certificate_bytes, 0o644)
        ledger_tmp = write_temp(ledger_dir, ledger_bytes, 0o644)
        os.replace(certificate_tmp, certificate_abs)
        certificate_tmp = None
        os.replace(ledger_tmp, ledger_abs)
        ledger_tmp = None
    except Exception:
        restore(certificate_abs, certificate_state)
        restore(ledger_abs, ledger_state)
        raise
    finally:
        for path in (certificate_tmp, ledger_tmp):
            if path and os.path.exists(path):
                os.unlink(path)


def build_outputs(batch, ledger_path):
    records, physical, tail = read_ledger(ledger_path)
    existing = {(row["batch_id"], row["sample_id"]): row for row in records}
    certificates = []
    appended = []
    counts = {"used": 0, "ambiguous": 0, "none": 0}
    for sample in batch["samples"]:
        result = reconstruct(sample)
        counts[result["status"]] += 1
        certificates.append(certificate_entry(sample, result))
        key = (batch["batch_id"], sample["sample_id"])
        digest = measurement_digest(sample)
        fraction = None
        if result["status"] == "used":
            fraction = fraction_text(result["candidates"][0])
        if key in existing:
            row = existing[key]
            if row["measurement_sha256"] != digest:
                raise ReviewError("same key changed measurement")
            if row["status"] != result["status"] or row["fraction"] != fraction:
                raise ReviewError("same key changed result")
            continue
        row = {
            "batch_id": batch["batch_id"],
            "sample_id": sample["sample_id"],
            "measurement_sha256": digest,
            "status": result["status"],
            "fraction": fraction,
            "prev_sha256": tail,
        }
        line = (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        appended.append(line)
        tail = hashlib.sha256(line).hexdigest()
    report = {
        "batch_id": batch["batch_id"],
        "samples": certificates,
        "summary": {
            "total": len(batch["samples"]),
            "used": counts["used"],
            "ambiguous": counts["ambiguous"],
            "none": counts["none"],
            "ledger_appended": len(appended),
        },
    }
    certificate_bytes = (
        json.dumps(report, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    old_bytes = b"".join(physical)
    return certificate_bytes, old_bytes + b"".join(appended)


def main(argv):
    if len(argv) != 4:
        return 2
    try:
        batch = read_batch(argv[1])
        certificate_bytes, ledger_bytes = build_outputs(batch, argv[3])
        publish(argv[2], argv[3], certificate_bytes, ledger_bytes)
    except ReviewError:
        sys.stderr.write("运行拒收: BAD_INPUT\n")
        return 1
    except Exception:
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

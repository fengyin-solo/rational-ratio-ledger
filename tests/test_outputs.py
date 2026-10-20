"""物性比值回填复核。"""

import hashlib
import json
import os
import random
import subprocess
import sys
import time
from fractions import Fraction
from pathlib import Path

import pytest


APP = Path(os.environ.get("TASK_APP", "/app/reconstruct_ratios.py"))
CALL_TIMEOUT_SEC = 45


def _sample(sample_id="M-2234", display="0.1429", digits=4, limit=100):
    return {
        "sample_id": sample_id,
        "display": display,
        "significant_digits": digits,
        "denominator_limit": limit,
    }


def _batch(batch_id="R260917-04", samples=None):
    return {"batch_id": batch_id, "samples": samples if samples is not None else [_sample()]}


def _write_batch(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _run(tmp_path, batch, name="case", certificate=None, ledger=None):
    batch_path = _write_batch(tmp_path / f"{name}.json", batch)
    certificate_path = certificate if certificate is not None else tmp_path / f"{name}.cert.json"
    ledger_path = ledger if ledger is not None else tmp_path / f"{name}.ledger.jsonl"
    finished = subprocess.run(
        [sys.executable, str(APP), str(batch_path), str(certificate_path), str(ledger_path)],
        capture_output=True,
        text=True,
        timeout=CALL_TIMEOUT_SEC,
    )
    return finished, certificate_path, ledger_path


def _certificate(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _measurement_digest(sample):
    ordered = {
        "denominator_limit": sample["denominator_limit"],
        "display": sample["display"],
        "sample_id": sample["sample_id"],
        "significant_digits": sample["significant_digits"],
    }
    raw = json.dumps(ordered, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _raw_line(record):
    return (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def _interval(certificate, index=0):
    return certificate["samples"][index]["interval"]


def _copy_bytes(path):
    return path.read_bytes()


def _unexpected_entries(directory, allowed):
    return sorted(path.name for path in directory.iterdir() if path.name not in allowed)


def test_used_sample_has_exact_certificate_shape_and_minimum_denominator(tmp_path):
    """「`used` 表示最小分母只对应一个分数，」"""
    finished, certificate_path, ledger_path = _run(tmp_path, _batch())
    certificate = _certificate(certificate_path)
    assert finished.returncode == 0
    assert certificate["samples"][0]["status"] == "used"
    assert certificate["samples"][0]["fraction"] == "1/7"
    assert certificate["samples"][0]["denominator"] == 7
    assert _records(ledger_path)[0]["fraction"] == "1/7"


def test_intro_example_uses_smallest_denominator(tmp_path):
    """显示值可能是 `0.1429`，配方库里对应的是 `1/7`。"""
    finished, certificate_path, _ = _run(
        tmp_path, _batch(batch_id="R260917-05", samples=[_sample()]), "intro"
    )
    item = _certificate(certificate_path)["samples"][0]
    assert finished.returncode == 0
    assert item["fraction"] == "1/7"


def test_original_recipe_batch_rerun_is_stable(tmp_path):
    """「夜班用它回填后，台账里还重复记过同一张单子。」"""
    batch = _batch(batch_id="R260917-06", samples=[_sample(sample_id="M-2235")])
    finished_first, _, ledger_path = _run(tmp_path, batch, "original")
    first_bytes = ledger_path.read_bytes()
    finished_second, _, ledger_path = _run(
        tmp_path, batch, "original", ledger=ledger_path
    )
    assert finished_first.returncode == 0
    assert finished_second.returncode == 0
    assert ledger_path.read_bytes() == first_bytes


def test_problem_description_cases_are_not_collapsed(tmp_path):
    """「遇到尾数是半格边界、负数或者同一批里有并列解释时，它就会给出互相打架的结果。」"""
    samples = [
        _sample(sample_id="M-2219", display="0.1255", digits=3, limit=8),
        _sample(sample_id="M-2220", display="-0.1255", digits=3, limit=8),
        _sample(sample_id="M-2221", display="10", digits=1, limit=1),
    ]
    finished, certificate_path, _ = _run(
        tmp_path, _batch(batch_id="R260917-07", samples=samples), "edge-cases"
    )
    items = _certificate(certificate_path)["samples"]
    assert finished.returncode == 0
    assert [item["status"] for item in items] == ["used", "used", "ambiguous"]
    assert items[0]["fraction"] == "1/8"
    assert items[1]["fraction"] == "-1/8"


def test_certificate_uses_compact_json_and_single_trailing_newline(tmp_path):
    """「证书要写成 UTF-8 的单行 JSON；键和值之间、相邻值之间都不留空格，文件最后只保留一个换行。」"""
    finished, certificate_path, _ = _run(tmp_path, _batch())
    raw = certificate_path.read_bytes()
    assert finished.returncode == 0
    assert raw.endswith(b"\n")
    assert not raw.endswith(b"\n\n")
    assert b": " not in raw
    assert b", " not in raw


def test_certificate_has_exactly_one_final_newline(tmp_path):
    """「证书要写成 UTF-8 的单行 JSON；键和值之间、相邻值之间都不留空格，文件最后只保留一个换行。」"""
    finished, certificate_path, _ = _run(tmp_path, _batch(), "newline")
    raw = certificate_path.read_bytes()
    assert finished.returncode == 0
    assert raw.endswith(b"\n")
    assert raw.count(b"\n") == 1


def test_certificate_top_level_and_summary_key_order(tmp_path):
    """顶层键依次是 `batch_id`、`samples`、`summary`。"""
    finished, certificate_path, _ = _run(tmp_path, _batch())
    certificate = _certificate(certificate_path)
    assert finished.returncode == 0
    assert list(certificate) == ["batch_id", "samples", "summary"]


def test_summary_reports_counts_and_key_order(tmp_path):
    """证书末尾的 `summary` 先报 `total`，随后依次报 `used`、`ambiguous`、`none`。最后放 `ledger_appended`。"""
    samples = [
        _sample(sample_id="M-2222", display="0.1429"),
        _sample(sample_id="M-2223", display="10", digits=1, limit=1),
        _sample(sample_id="M-2224", display="0.1", digits=1, limit=2),
    ]
    finished, certificate_path, _ = _run(
        tmp_path, _batch(batch_id="R260917-08", samples=samples), "summary"
    )
    summary = _certificate(certificate_path)["summary"]
    assert finished.returncode == 0
    assert list(summary) == ["total", "used", "ambiguous", "none", "ledger_appended"]
    assert summary == {
        "total": 3,
        "used": 1,
        "ambiguous": 1,
        "none": 1,
        "ledger_appended": 3,
    }


def test_summary_ledger_appended_counts_only_new_lines(tmp_path):
    """「这个字段只数这次新写进台账的几行。」"""
    batch = _batch(batch_id="R260917-09", samples=[_sample()])
    finished_first, certificate_path, ledger_path = _run(tmp_path, batch, "append-count")
    first_summary = _certificate(certificate_path)["summary"]
    finished_second, certificate_path, ledger_path = _run(
        tmp_path, batch, "append-count", certificate=certificate_path, ledger=ledger_path
    )
    second_summary = _certificate(certificate_path)["summary"]
    assert finished_first.returncode == 0
    assert finished_second.returncode == 0
    assert first_summary["ledger_appended"] == 1
    assert second_summary["ledger_appended"] == 0


def test_interval_key_order_and_rational_endpoint_text(tmp_path):
    """`interval` 先写 `lo`，再写 `hi`。接着写 `include_lo`，最后写 `include_hi`。"""
    sample = _sample(display="0.1255", digits=3, limit=8)
    finished, certificate_path, _ = _run(tmp_path, _batch(samples=[sample]), "interval-shape")
    interval = _interval(_certificate(certificate_path))
    assert finished.returncode == 0
    assert list(interval) == ["lo", "hi", "include_lo", "include_hi"]


def test_none_entry_has_exactly_three_keys(tmp_path):
    """`none` 项只有 `sample_id`、`status`、`interval` 三个键。"""
    sample = _sample(display="0.1", digits=1, limit=2)
    finished, certificate_path, _ = _run(tmp_path, _batch(samples=[sample]), "none-keys")
    item = _certificate(certificate_path)["samples"][0]
    assert finished.returncode == 0
    assert list(item) == ["sample_id", "status", "interval"]


def test_none_entry_has_no_result_fields(tmp_path):
    """`none` 项只有 `sample_id`、`status`、`interval` 三个键。"""
    sample = _sample(display="0.1", digits=1, limit=2)
    finished, certificate_path, _ = _run(tmp_path, _batch(samples=[sample]), "none-fields")
    item = _certificate(certificate_path)["samples"][0]
    assert finished.returncode == 0
    assert "fraction" not in item
    assert "denominator" not in item


def test_positive_lower_endpoint_is_included(tmp_path):
    """「正数的下边界算在区间内、上边界不算；」"""
    sample = _sample(display="0.1255", digits=3, limit=8)
    finished, certificate_path, _ = _run(tmp_path, _batch(samples=[sample]), "positive-lower")
    item = _certificate(certificate_path)["samples"][0]
    assert finished.returncode == 0
    assert item["fraction"] == "1/8"
    assert item["interval"]["include_lo"] is True
    assert item["interval"]["include_hi"] is False


def test_reading_step_uses_half_of_last_decimal_place(tmp_path):
    """`display` 的小数位允许比 `significant_digits` 多，真正决定刻度的是声明的有效位数。"""
    sample = _sample(display="0.1255", digits=3, limit=8)
    finished, certificate_path, _ = _run(tmp_path, _batch(samples=[sample]), "step")
    interval = _interval(_certificate(certificate_path))
    assert finished.returncode == 0
    assert interval["lo"] == "1/8"
    assert interval["hi"] == "63/500"


def test_exact_decimal_endpoint_comparison_keeps_boundary_fraction(tmp_path):
    """「这里的所有边界都要按十进制原值精确比较，不能先转成二进制浮点数。」"""
    sample = _sample(display="0.1255", digits=3, limit=8)
    finished, certificate_path, _ = _run(tmp_path, _batch(samples=[sample]), "exact-endpoint")
    item = _certificate(certificate_path)["samples"][0]
    assert finished.returncode == 0
    assert item["fraction"] == "1/8"


def test_interval_endpoint_strings_are_reduced_fractions(tmp_path):
    """「两个端点保存为最简有理数的文本：整数就直接写，其余写成 `分子/分母`，分母一律为正。」"""
    sample = _sample(display="0.1255", digits=3, limit=8)
    finished, certificate_path, _ = _run(tmp_path, _batch(samples=[sample]), "reduced")
    interval = _interval(_certificate(certificate_path))
    assert finished.returncode == 0
    assert interval["lo"] == "1/8"
    assert interval["hi"] == "63/500"


def test_positive_upper_endpoint_is_excluded(tmp_path):
    """「正数的下边界算在区间内、上边界不算；」"""
    sample = _sample(display="0.1245", digits=3, limit=8)
    finished, certificate_path, _ = _run(tmp_path, _batch(samples=[sample]), "positive-upper")
    item = _certificate(certificate_path)["samples"][0]
    assert finished.returncode == 0
    assert item["status"] == "none"
    assert item["interval"]["hi"] == "1/8"


def test_negative_upper_endpoint_is_included(tmp_path):
    """「负数的上边界算在区间内、下边界不算。」"""
    sample = _sample(display="-0.1255", digits=3, limit=8)
    finished, certificate_path, _ = _run(tmp_path, _batch(samples=[sample]), "negative-upper")
    item = _certificate(certificate_path)["samples"][0]
    assert finished.returncode == 0
    assert item["fraction"] == "-1/8"
    assert item["interval"]["include_lo"] is False
    assert item["interval"]["include_hi"] is True


def test_negative_lower_endpoint_is_excluded(tmp_path):
    """「负数的上边界算在区间内、下边界不算。」"""
    sample = _sample(display="-0.1245", digits=3, limit=8)
    finished, certificate_path, _ = _run(tmp_path, _batch(samples=[sample]), "negative-lower")
    item = _certificate(certificate_path)["samples"][0]
    assert finished.returncode == 0
    assert item["status"] == "none"
    assert item["interval"]["lo"] == "-1/8"


def test_ambiguous_reports_all_candidates_at_minimum_denominator(tmp_path):
    """`ambiguous` 表示同一最小分母下有多个分数。"""
    sample = _sample(display="10", digits=1, limit=1)
    finished, certificate_path, ledger_path = _run(
        tmp_path, _batch(samples=[sample]), "ambiguous"
    )
    item = _certificate(certificate_path)["samples"][0]
    assert finished.returncode == 0
    assert item["status"] == "ambiguous"
    assert item["denominator"] == 1
    assert item["candidates"] == [str(value) for value in range(5, 15)]
    assert _records(ledger_path)[0]["status"] == "ambiguous"


def test_no_candidate_is_reported_as_none(tmp_path):
    """「`none` 表示分母上限内没有候选。」"""
    sample = _sample(display="0.1", digits=1, limit=2)
    finished, certificate_path, ledger_path = _run(tmp_path, _batch(samples=[sample]), "none")
    item = _certificate(certificate_path)["samples"][0]
    assert finished.returncode == 0
    assert item["status"] == "none"
    assert set(item) == {"sample_id", "status", "interval"}
    assert _records(ledger_path)[0]["fraction"] is None


def test_denominator_limit_is_inclusive(tmp_path):
    """分母从 1 数到 `denominator_limit`。"""
    sample_at_limit = _sample(display="0.1429", digits=4, limit=7)
    sample_below_limit = _sample(display="0.1429", digits=4, limit=6)
    finished_at, certificate_at, _ = _run(
        tmp_path, _batch(batch_id="R260917-10", samples=[sample_at_limit]), "at-limit"
    )
    finished_below, certificate_below, _ = _run(
        tmp_path, _batch(batch_id="R260917-11", samples=[sample_below_limit]), "below-limit"
    )
    assert finished_at.returncode == 0
    assert finished_below.returncode == 0
    assert _certificate(certificate_at)["samples"][0]["fraction"] == "1/7"
    assert _certificate(certificate_below)["samples"][0]["status"] == "none"


def test_huge_limit_still_reports_smallest_denominator(tmp_path):
    """上限开到一万亿也不改变“最小分母”这个判决，`0.1429` 仍然回填 `1/7`。"""
    sample = _sample(display="0.1429", digits=4, limit=10**12)
    finished, certificate_path, _ = _run(tmp_path, _batch(samples=[sample]), "huge-limit")
    item = _certificate(certificate_path)["samples"][0]
    assert finished.returncode == 0
    assert item["status"] == "used"
    assert item["fraction"] == "1/7"
    assert item["denominator"] == 7


def test_precise_reading_recovers_large_denominator(tmp_path):
    """「配方库升级以后，读数可以细到十五位有效数字，分母上限会开到一万亿，单批最多两千条。」"""
    sample = _sample(display="0.00000123456789012", digits=15, limit=10**12)
    finished, certificate_path, ledger_path = _run(tmp_path, _batch(samples=[sample]), "precise")
    item = _certificate(certificate_path)["samples"][0]
    assert finished.returncode == 0
    assert item["status"] == "used"
    assert item["denominator"] == 4220910038
    assert item["fraction"] == "5211/4220910038"
    assert _records(ledger_path)[0]["fraction"] == "5211/4220910038"


def test_precise_reading_without_candidate_stays_none(tmp_path):
    """「区间里没有任何这样的分数时，样本记为没有候选。」"""
    sample = _sample(display="9.99999999999999", digits=15, limit=10**12)
    finished, certificate_path, _ = _run(tmp_path, _batch(samples=[sample]), "precise-none")
    item = _certificate(certificate_path)["samples"][0]
    assert finished.returncode == 0
    assert item["status"] == "none"
    assert set(item) == {"sample_id", "status", "interval"}


def test_negative_precise_reading_keeps_sign_and_denominator(tmp_path):
    """「正数的下边界算在区间内、上边界不算；负数的上边界算在区间内、下边界不算。」"""
    sample = _sample(display="-0.577215664901533", digits=15, limit=10**12)
    finished, certificate_path, _ = _run(tmp_path, _batch(samples=[sample]), "precise-negative")
    item = _certificate(certificate_path)["samples"][0]
    assert finished.returncode == 0
    assert item["status"] == "used"
    assert item["denominator"] == 15700603
    assert item["fraction"] == "-9062634/15700603"


def test_large_batch_finishes_within_the_shift_window(tmp_path):
    """「夜班要求整批复核在普通工作站上 60 秒内跑完：上限只是判决边界，不能把它当成循环次数，逐个分母试过去的写法撑不住这样的批次。」"""
    rng = random.Random(20261019)
    samples = []
    for index in range(300):
        whole = rng.randint(1, 9)
        fraction_digits = "".join(str(rng.randint(0, 9)) for _ in range(14))
        sign = "-" if rng.random() < 0.25 else ""
        samples.append(
            _sample(
                sample_id=f"M-S{index + 1:04d}",
                display=f"{sign}{whole}.{fraction_digits}",
                digits=15,
                limit=10**12,
            )
        )
    started = time.monotonic()
    finished, certificate_path, ledger_path = _run(
        tmp_path, _batch(batch_id="R261019-30", samples=samples), "large-batch"
    )
    elapsed = time.monotonic() - started
    certificate = _certificate(certificate_path)
    assert finished.returncode == 0
    assert elapsed < 40
    assert certificate["summary"]["total"] == 300
    assert certificate["summary"]["used"] == 300
    assert certificate["summary"]["ledger_appended"] == 300
    assert certificate["samples"][0]["fraction"] == "49494709/8063859"
    assert certificate["samples"][7]["fraction"] == "-17000876/11174169"
    assert certificate["samples"][299]["fraction"] == "55915120/6258909"
    assert len(_records(ledger_path)) == 300


def test_batch_size_cap_is_enforced(tmp_path):
    """「`samples` 至少有一条、最多两千条，顺序就是收样顺序。」"""
    samples = [
        _sample(sample_id=f"M-C{index:04d}", display="0.1429") for index in range(2001)
    ]
    finished, certificate_path, ledger_path = _run(tmp_path, _batch(samples=samples), "too-many")
    assert finished.returncode == 1
    assert finished.stderr.startswith("运行拒收: BAD_INPUT")
    assert not certificate_path.exists()
    assert not ledger_path.exists()


def test_same_batch_rerun_is_idempotent(tmp_path):
    """同一份收样单和同一份台账连续跑两遍，第二遍的证书条目与第一遍相同、`ledger_appended` 为 0，台账字节不再变化。"""
    finished_first, certificate_path, ledger_path = _run(
        tmp_path, _batch(batch_id="R260917-12", samples=[_sample()]), "same"
    )
    certificate_first = _copy_bytes(certificate_path)
    ledger_first = _copy_bytes(ledger_path)
    samples_first = _certificate(certificate_path)["samples"]
    finished_second, certificate_path, ledger_path = _run(
        tmp_path, _batch(batch_id="R260917-12", samples=[_sample()]), "same"
    )
    assert finished_first.returncode == 0
    assert finished_second.returncode == 0
    assert _certificate(certificate_path)["samples"] == samples_first
    assert ledger_path.read_bytes() == ledger_first
    assert _certificate(certificate_path)["summary"]["ledger_appended"] == 0


def test_same_sample_in_new_batch_appends_a_new_version(tmp_path):
    """同一 `sample_id` 出现在新的批次时允许新增一条版本。"""
    first = _batch(batch_id="R260917-13", samples=[_sample(display="0.1429")])
    second = _batch(batch_id="R260917-14", samples=[_sample(display="0.2857")])
    finished_first, _, ledger_path = _run(tmp_path, first, "first")
    finished_second, certificate_path, ledger_path = _run(
        tmp_path, second, "second", ledger=ledger_path
    )
    records = _records(ledger_path)
    assert finished_first.returncode == 0
    assert finished_second.returncode == 0
    assert len(records) == 2
    assert records[0]["batch_id"] == "R260917-13"
    assert records[1]["batch_id"] == "R260917-14"
    assert _certificate(certificate_path)["summary"]["ledger_appended"] == 1


def test_ledger_chain_links_each_line_to_the_previous_line(tmp_path):
    """`prev_sha256` 是上一行原始字节加一个换行后的 SHA-256。"""
    first = _batch(batch_id="R260917-15", samples=[_sample()])
    second = _batch(batch_id="R260917-16", samples=[_sample(display="0.2857")])
    finished_first, _, ledger_path = _run(tmp_path, first, "chain-a")
    finished_second, _, ledger_path = _run(tmp_path, second, "chain-b", ledger=ledger_path)
    lines = ledger_path.read_bytes().splitlines(keepends=True)
    records = _records(ledger_path)
    assert finished_first.returncode == 0
    assert finished_second.returncode == 0
    assert records[0]["prev_sha256"] == "0" * 64
    assert records[1]["prev_sha256"] == hashlib.sha256(lines[0]).hexdigest()


def test_ledger_record_key_order_is_fixed(tmp_path):
    """共享台账采用 JSONL：每行一条单行 JSON，先记 `batch_id`，再记 `sample_id`，接着记 `measurement_sha256`、`status`、`fraction`，末尾是 `prev_sha256`。"""
    finished, _, ledger_path = _run(
        tmp_path, _batch(batch_id="R260917-17", samples=[_sample()]), "ledger-keys"
    )
    record = _records(ledger_path)[0]
    assert finished.returncode == 0
    assert list(record) == [
        "batch_id",
        "sample_id",
        "measurement_sha256",
        "status",
        "fraction",
        "prev_sha256",
    ]


def test_measurement_digest_uses_sorted_sample_object(tmp_path):
    """`measurement_sha256` 是样本对象按 `denominator_limit`、`display`、`sample_id`、`significant_digits` 键名排序后的紧凑 JSON 字节摘要。"""
    sample = _sample(sample_id="M-2225", display="0.1429", digits=4, limit=100)
    finished, _, ledger_path = _run(tmp_path, _batch(samples=[sample]), "digest")
    record = _records(ledger_path)[0]
    assert finished.returncode == 0
    assert record["measurement_sha256"] == _measurement_digest(sample)


def test_measurement_digest_changes_with_significant_digits(tmp_path):
    """`measurement_sha256` 是样本对象按 `denominator_limit`、`display`、`sample_id`、`significant_digits` 键名排序后的紧凑 JSON 字节摘要。"""
    first = _sample(sample_id="M-2226", display="0.1429", digits=4, limit=100)
    second = _sample(sample_id="M-2226", display="0.1429", digits=3, limit=100)
    finished_first, _, ledger_first = _run(
        tmp_path, _batch(batch_id="R260917-18", samples=[first]), "digest-a"
    )
    finished_second, _, ledger_second = _run(
        tmp_path, _batch(batch_id="R260917-19", samples=[second]), "digest-b"
    )
    assert finished_first.returncode == 0
    assert finished_second.returncode == 0
    assert _records(ledger_first)[0]["measurement_sha256"] != _records(ledger_second)[0][
        "measurement_sha256"
    ]


@pytest.mark.parametrize(
    "display,case_id",
    [
        ("+0.1", "DISPLAY-01"),
        ("1e-3", "DISPLAY-02"),
        (" 0.1", "DISPLAY-03"),
        (".5", "DISPLAY-04"),
        ("1.", "DISPLAY-05"),
        ("0", "DISPLAY-06"),
        ("-0.0", "DISPLAY-07"),
        ("NaN", "DISPLAY-08"),
        ("Infinity", "DISPLAY-09"),
    ],
)
def test_invalid_display_is_rejected(tmp_path, display, case_id):
    """「不能有正号、指数、`NaN`、无穷、首尾空白或孤零零的小数点；小数点前后都必须有数字，读数不能是零。」"""
    print(f"CASE {case_id}")
    sample = _sample(display=display)
    finished, certificate_path, ledger_path = _run(
        tmp_path, _batch(samples=[sample]), f"display-{case_id}"
    )
    assert finished.returncode != 0
    assert not certificate_path.exists()
    assert not ledger_path.exists()


@pytest.mark.parametrize(
    "digits,case_id,expected_rc",
    [
        (0, "SIG-01", 1),
        (16, "SIG-02", 1),
        (True, "SIG-03", 1),
        (1, "SIG-04", 0),
    ],
)
def test_significant_digits_bounds(tmp_path, digits, case_id, expected_rc):
    """`significant_digits` 是 1 到 15 的整数。"""
    print(f"CASE {case_id}")
    sample = _sample(digits=digits)
    finished, certificate_path, _ = _run(
        tmp_path, _batch(samples=[sample]), f"sig-{case_id}"
    )
    assert (finished.returncode == 0) is (expected_rc == 0)
    assert (certificate_path.exists()) is (expected_rc == 0)


@pytest.mark.parametrize(
    "limit,case_id,expected_rc",
    [
        (0, "LIMIT-01", 1),
        (10**12 + 1, "LIMIT-02", 1),
        (True, "LIMIT-03", 1),
        (10**12, "LIMIT-04", 0),
    ],
)
def test_denominator_limit_bounds(tmp_path, limit, case_id, expected_rc):
    """`denominator_limit` 是 1 到 1000000000000 的整数，布尔值不算整数。"""
    print(f"CASE {case_id}")
    sample = _sample(limit=limit)
    finished, certificate_path, _ = _run(
        tmp_path, _batch(samples=[sample]), f"limit-{case_id}"
    )
    assert (finished.returncode == 0) is (expected_rc == 0)
    assert (certificate_path.exists()) is (expected_rc == 0)


def test_duplicate_sample_id_is_rejected(tmp_path):
    """同一份单子里 `sample_id` 重复。"""
    samples = [_sample(sample_id="M-2227"), _sample(sample_id="M-2227", display="0.2857")]
    finished, certificate_path, ledger_path = _run(tmp_path, _batch(samples=samples), "dup-id")
    assert finished.returncode != 0
    assert not certificate_path.exists()
    assert not ledger_path.exists()


def test_duplicate_json_key_is_rejected(tmp_path):
    """「JSON 里有重复键、」"""
    raw = '{"batch_id":"DUPKEY","batch_id":"DUPKEY-2","samples":[]}'
    batch_path = tmp_path / "duplicate.json"
    batch_path.write_text(raw, encoding="utf-8")
    certificate_path = tmp_path / "duplicate.cert.json"
    ledger_path = tmp_path / "duplicate.ledger.jsonl"
    finished = subprocess.run(
        [sys.executable, str(APP), str(batch_path), str(certificate_path), str(ledger_path)],
        capture_output=True,
        text=True,
        timeout=CALL_TIMEOUT_SEC,
    )
    assert finished.returncode != 0
    assert not certificate_path.exists()
    assert not ledger_path.exists()


def test_extra_top_level_key_is_rejected(tmp_path):
    """内容顶层只有 `batch_id` 和 `samples` 两项。"""
    batch = _batch()
    batch["note"] = "not part of the contract"
    finished, certificate_path, _ = _run(tmp_path, batch, "extra-top")
    assert finished.returncode != 0
    assert not certificate_path.exists()


def test_extra_sample_key_is_rejected(tmp_path):
    """每条样本恰好有 `sample_id`、`display`、`significant_digits`、`denominator_limit` 四个键。"""
    sample = _sample()
    sample["ticket"] = "R-18"
    finished, certificate_path, _ = _run(tmp_path, _batch(samples=[sample]), "extra-sample")
    assert finished.returncode != 0
    assert not certificate_path.exists()


def test_missing_sample_key_is_rejected(tmp_path):
    """每条样本恰好有 `sample_id`、`display`、`significant_digits`、`denominator_limit` 四个键。"""
    sample = _sample()
    sample.pop("denominator_limit")
    finished, certificate_path, _ = _run(tmp_path, _batch(samples=[sample]), "case-11")
    assert finished.returncode != 0
    assert not certificate_path.exists()


def test_empty_samples_is_rejected(tmp_path):
    """`samples` 至少有一条，顺序就是收样顺序。"""
    finished, certificate_path, _ = _run(tmp_path, _batch(samples=[]), "empty")
    assert finished.returncode != 0
    assert not certificate_path.exists()


def test_batch_id_with_surrounding_space_is_rejected(tmp_path):
    """`batch_id` 是非空字符串，不带首尾空白。"""
    finished, certificate_path, _ = _run(
        tmp_path, _batch(batch_id=" BATCH-SPACE "), "batch-space"
    )
    assert finished.returncode != 0
    assert not certificate_path.exists()


def test_display_magnitude_boundary_is_enforced(tmp_path):
    """「绝对值不能超过 1000。」"""
    at_bound = _sample(display="1000", digits=1, limit=1)
    above_bound = _sample(sample_id="M-2228", display="1000.1", digits=1, limit=1)
    finished_at, certificate_at, _ = _run(
        tmp_path, _batch(batch_id="MAG-AT", samples=[at_bound]), "mag-at"
    )
    finished_above, certificate_above, _ = _run(
        tmp_path, _batch(batch_id="MAG-ABOVE", samples=[above_bound]), "mag-above"
    )
    assert finished_at.returncode == 0
    assert finished_above.returncode != 0
    assert certificate_at.exists()
    assert not certificate_above.exists()


def test_wrong_argument_count_is_rejected(tmp_path):
    """「三个位置参数少一个或多一个都按用法错误结束，退出码非零。」"""
    batch_path = _write_batch(tmp_path / "args.json", _batch())
    short = subprocess.run(
        [sys.executable, str(APP), str(batch_path)],
        capture_output=True,
        text=True,
        timeout=CALL_TIMEOUT_SEC,
    )
    long = subprocess.run(
        [
            sys.executable,
            str(APP),
            str(batch_path),
            str(tmp_path / "args.cert.json"),
            str(tmp_path / "args.ledger.jsonl"),
            "extra",
        ],
        capture_output=True,
        text=True,
        timeout=CALL_TIMEOUT_SEC,
    )
    assert short.returncode != 0
    assert long.returncode != 0


def test_changed_measurement_for_existing_key_is_rejected(tmp_path):
    """「同一对键出现不同测量摘要时，说明同一张单子被改过，整次复核退回。」"""
    first = _batch(batch_id="R260917-20", samples=[_sample(display="0.1429")])
    changed = _batch(batch_id="R260917-20", samples=[_sample(display="0.2857")])
    finished_first, certificate_path, ledger_path = _run(tmp_path, first, "changed")
    certificate_before = _copy_bytes(certificate_path)
    ledger_before = _copy_bytes(ledger_path)
    finished_changed, certificate_path, ledger_path = _run(
        tmp_path, changed, "changed", certificate=certificate_path, ledger=ledger_path
    )
    assert finished_first.returncode == 0
    assert finished_changed.returncode != 0
    assert certificate_path.read_bytes() == certificate_before
    assert ledger_path.read_bytes() == ledger_before


def test_broken_previous_digest_is_rejected(tmp_path):
    """台账里出现坏行、重复键、摘要格式不对、`prev_sha256` 接不上，或者同一条记录出现两次，整次复核同样退回。"""
    first = _batch(batch_id="R260918-01", samples=[_sample()])
    finished_first, certificate_path, ledger_path = _run(tmp_path, first, "case-21")
    records = _records(ledger_path)
    bad = dict(records[0])
    bad["batch_id"] = "R260918-02"
    bad["sample_id"] = "S-B"
    bad["prev_sha256"] = "0" * 64
    ledger_path.write_bytes(ledger_path.read_bytes() + _raw_line(bad))
    certificate_before = _copy_bytes(certificate_path)
    second = _batch(batch_id="R260918-03", samples=[_sample(sample_id="M-2229")])
    finished_second, certificate_path, ledger_path = _run(
        tmp_path, second, "case-22", certificate=certificate_path, ledger=ledger_path
    )
    assert finished_first.returncode == 0
    assert finished_second.returncode != 0
    assert certificate_path.read_bytes() == certificate_before
    assert _records(ledger_path)[-1]["prev_sha256"] == "0" * 64


def test_duplicate_ledger_key_is_rejected(tmp_path):
    """台账里出现坏行、重复键、摘要格式不对、`prev_sha256` 接不上，或者同一条记录出现两次，整次复核同样退回。"""
    first = _batch(batch_id="R260918-04", samples=[_sample()])
    finished_first, certificate_path, ledger_path = _run(tmp_path, first, "dupledger")
    lines = ledger_path.read_bytes().splitlines(keepends=True)
    duplicate = json.loads(lines[0])
    duplicate["prev_sha256"] = hashlib.sha256(lines[0]).hexdigest()
    ledger_path.write_bytes(ledger_path.read_bytes() + _raw_line(duplicate))
    certificate_before = _copy_bytes(certificate_path)
    second = _batch(batch_id="R260918-05", samples=[_sample(sample_id="M-2230")])
    finished_second, certificate_path, ledger_path = _run(
        tmp_path, second, "dupledger-2", certificate=certificate_path, ledger=ledger_path
    )
    assert finished_first.returncode == 0
    assert finished_second.returncode != 0
    assert certificate_path.read_bytes() == certificate_before


def test_bad_ledger_digest_is_rejected(tmp_path):
    """台账里出现坏行、重复键、摘要格式不对、`prev_sha256` 接不上，或者同一条记录出现两次，整次复核同样退回。"""
    first = _batch(batch_id="R260918-06", samples=[_sample()])
    finished_first, certificate_path, ledger_path = _run(tmp_path, first, "case-23")
    record = _records(ledger_path)[0]
    record["measurement_sha256"] = "G" * 64
    ledger_path.write_bytes(_raw_line(record))
    certificate_before = _copy_bytes(certificate_path)
    second = _batch(batch_id="R260918-07", samples=[_sample(sample_id="M-2230")])
    finished_second, certificate_path, ledger_path = _run(
        tmp_path, second, "case-24", certificate=certificate_path, ledger=ledger_path
    )
    assert finished_first.returncode == 0
    assert finished_second.returncode != 0
    assert certificate_path.read_bytes() == certificate_before


def test_zero_denominator_in_ledger_is_rejected(tmp_path):
    """「台账里出现坏行、重复键、摘要格式不对、」"""
    first = _batch(batch_id="R260918-15", samples=[_sample(sample_id="M-2234")])
    finished_first, certificate_path, ledger_path = _run(tmp_path, first, "case-26")
    record = _records(ledger_path)[0]
    record["fraction"] = "1/0"
    ledger_path.write_bytes(_raw_line(record))
    certificate_before = _copy_bytes(certificate_path)
    second = _batch(batch_id="R260918-16", samples=[_sample(sample_id="M-2235")])
    finished_second, certificate_path, ledger_path = _run(
        tmp_path, second, "case-27", certificate=certificate_path, ledger=ledger_path
    )
    assert finished_first.returncode == 0
    assert finished_second.returncode == 1
    assert finished_second.stderr.startswith("运行拒收: BAD_INPUT")
    assert "traceback" not in finished_second.stderr.lower()
    assert certificate_path.read_bytes() == certificate_before


def test_invalid_batch_keeps_existing_certificate_and_ledger(tmp_path):
    """「退出时证书和台账都保持调用前的字节，目录里不能留下半截新文件或临时文件。」"""
    valid = _batch(batch_id="R260918-08", samples=[_sample()])
    finished_valid, certificate_path, ledger_path = _run(tmp_path, valid, "keep")
    certificate_before = _copy_bytes(certificate_path)
    ledger_before = _copy_bytes(ledger_path)
    invalid = _batch(batch_id="R260918-09", samples=[_sample(display="not-a-number")])
    invalid_path = _write_batch(tmp_path / "case-25.json", invalid)
    finished_invalid = subprocess.run(
        [
            sys.executable,
            str(APP),
            str(invalid_path),
            str(certificate_path),
            str(ledger_path),
        ],
        capture_output=True,
        text=True,
        timeout=CALL_TIMEOUT_SEC,
    )
    assert finished_valid.returncode == 0
    assert finished_invalid.returncode != 0
    assert certificate_path.read_bytes() == certificate_before
    assert ledger_path.read_bytes() == ledger_before
    assert _unexpected_entries(
        tmp_path,
        {"keep.json", "keep.cert.json", "keep.ledger.jsonl", "case-25.json"},
    ) == []


def test_ledger_target_directory_is_rejected_without_replacing_certificate(tmp_path):
    """「任何校验、写入或替换失败时，两个文件都保持调用前的字节。」"""
    certificate_path = tmp_path / "target.cert.json"
    ledger_path = tmp_path / "target.ledger.jsonl"
    certificate_path.write_bytes(b"old-certificate\n")
    ledger_path.mkdir()
    finished, _, _ = _run(
        tmp_path,
        _batch(batch_id="R260918-10", samples=[_sample()]),
        "target",
        certificate=certificate_path,
        ledger=ledger_path,
    )
    assert finished.returncode != 0
    assert certificate_path.read_bytes() == b"old-certificate\n"
    assert ledger_path.is_dir()
    assert _unexpected_entries(
        tmp_path,
        {"target.json", "target.cert.json", "target.ledger.jsonl"},
    ) == []


def test_successful_publish_leaves_no_temporary_files(tmp_path):
    """「目录里不能留下半截新文件或临时文件。」"""
    finished, certificate_path, ledger_path = _run(
        tmp_path, _batch(batch_id="R260918-15", samples=[_sample()]), "clean-temp"
    )
    assert finished.returncode == 0
    assert _unexpected_entries(
        tmp_path,
        {"clean-temp.json", "clean-temp.cert.json", "clean-temp.ledger.jsonl"},
    ) == []
    assert certificate_path.is_file()
    assert ledger_path.is_file()


def test_failed_publish_leaves_no_temporary_files(tmp_path):
    """「退出时证书和台账都保持调用前的字节，目录里不能留下半截新文件或临时文件。」"""
    certificate_path = tmp_path / "failed.cert.json"
    ledger_path = tmp_path / "failed.ledger.jsonl"
    certificate_path.write_bytes(b"old\n")
    ledger_path.write_bytes(b"")
    batch = _batch(batch_id="R260918-16", samples=[_sample(display="0.1", digits=0)])
    finished, _, _ = _run(
        tmp_path,
        batch,
        "failed-temp",
        certificate=certificate_path,
        ledger=ledger_path,
    )
    assert finished.returncode != 0
    assert certificate_path.read_bytes() == b"old\n"
    assert ledger_path.read_bytes() == b""
    assert _unexpected_entries(
        tmp_path,
        {"failed-temp.json", "failed.cert.json", "failed.ledger.jsonl"},
    ) == []


def test_successful_publish_writes_certificate_and_ledger_together(tmp_path):
    """「两份新内容都准备好之后才替换旧文件；」"""
    finished, certificate_path, ledger_path = _run(
        tmp_path, _batch(batch_id="R260918-13", samples=[_sample()]), "together"
    )
    certificate = _certificate(certificate_path)
    records = _records(ledger_path)
    assert finished.returncode == 0
    assert certificate["batch_id"] == "R260918-13"
    assert records[0]["batch_id"] == "R260918-13"


def test_mixed_batch_preserves_order_and_chains_three_statuses(tmp_path):
    """`samples` 保持收样顺序；每项先放 `sample_id`、`status`，再放 `interval`。"""
    samples = [
        _sample(sample_id="M-2231", display="0.1429"),
        _sample(sample_id="M-2232", display="10", digits=1, limit=1),
        _sample(sample_id="M-2233", display="0.1", digits=1, limit=2),
    ]
    batch = _batch(batch_id="R260918-14", samples=samples)
    finished_first, certificate_path, ledger_path = _run(tmp_path, batch, "mixed")
    certificate = _certificate(certificate_path)
    lines = ledger_path.read_bytes().splitlines(keepends=True)
    records = _records(ledger_path)
    finished_second, certificate_path, ledger_path = _run(
        tmp_path, batch, "mixed", certificate=certificate_path, ledger=ledger_path
    )
    assert finished_first.returncode == 0
    assert finished_second.returncode == 0
    assert [item["sample_id"] for item in certificate["samples"]] == ["M-2231", "M-2232", "M-2233"]
    assert [item["status"] for item in certificate["samples"]] == ["used", "ambiguous", "none"]
    assert len(records) == 3
    assert records[0]["prev_sha256"] == "0" * 64
    assert records[1]["prev_sha256"] == hashlib.sha256(lines[0]).hexdigest()
    assert records[2]["prev_sha256"] == hashlib.sha256(lines[1]).hexdigest()

"""
Test suite for Job ID extraction and Deduplication (job_store).

Verifies:
  1. _extract_job_id() handles all LinkedIn URL variants, query params, and URNs
  2. job_store.filter_new() checks exact Job ID
  3. job_store.filter_new() checks title+company fingerprint (reposted under new ID)
  4. job_store.mark_seen_batch() preserves timestamps and records fingerprints
  5. job_store.cleanup() respects the TTL (30 days) and handles legacy string entries
"""
import os
import shutil
import tempfile
from datetime import datetime, timezone, timedelta

import constants
import job_store
from linkedin_jobs import _extract_job_id

def test_extract_job_id():
    test_cases = [
        # (input_url, expected_id, label)
        ("https://www.linkedin.com/jobs/view/4481511083/", "4481511083", "view url with trailing slash"),
        ("https://in.linkedin.com/jobs/view/devops-engineer-at-wipro-4481511083", "4481511083", "slug url in sub-domain without trailing slash"),
        ("https://www.linkedin.com/jobs/view/devops-engineer-4481511083?refId=abc&trackingId=def", "4481511083", "slug with query params"),
        ("https://www.linkedin.com/jobs/search/?currentJobId=4481511083&keywords=devops", "4481511083", "search url with currentJobId query param"),
        ("https://www.linkedin.com/jobs/search/?jobId=4481511083", "4481511083", "search url with jobId query param"),
        ("urn:li:jobPosting:4481511083", "4481511083", "data-entity-urn attribute format"),
        ("/jobs/view/4481511083?refId=123", "4481511083", "relative url"),
        ("4481511083", "4481511083", "raw numeric id"),
    ]

    print("\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print("SECTION 1 — _extract_job_id() tests")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    passed = 0
    for inp, expected, label in test_cases:
        actual = _extract_job_id(inp)
        assert actual == expected, f"Failed [{label}]: expected {expected}, got {actual}"
        print(f"  [PASS] {label}: {inp} → {actual}")
        passed += 1
    print(f"  Result: {passed}/{len(test_cases)} passed")


def test_job_store_dedup():
    print("\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print("SECTION 2 — job_store dual-layer dedup tests")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    # Use a temporary file for the store
    temp_dir = tempfile.mkdtemp()
    temp_store_file = os.path.join(temp_dir, "test_seen_jobs.json")
    orig_store_path = job_store._STORE_PATH
    job_store._STORE_PATH = temp_store_file

    try:
        # 1. Fresh store: all jobs are new
        jobs = [
            {"id": "1001", "title": "DevOps Engineer", "company": "Wipro", "location": "Bengaluru"},
            {"id": "1002", "title": "SRE Lead", "company": "Infosys", "location": "Pune"},
        ]
        ids = [j["id"] for j in jobs]
        new_ids = job_store.filter_new(ids, jobs=jobs)
        assert new_ids == ["1001", "1002"], f"Expected all new, got {new_ids}"
        print("  [PASS] Empty store returns all jobs as new")

        # 2. Mark seen
        job_store.mark_seen_batch(ids, jobs=jobs)
        assert not job_store.is_new("1001")
        assert not job_store.is_new("1002")
        print("  [PASS] Mark seen persists entries to store")

        # 3. Exact ID match filter
        check_ids = ["1001", "1003"]
        check_jobs = [
            {"id": "1001", "title": "DevOps Engineer", "company": "Wipro"},
            {"id": "1003", "title": "Cloud Architect", "company": "TCS"},
        ]
        new_ids = job_store.filter_new(check_ids, jobs=check_jobs)
        assert new_ids == ["1003"], f"Expected ['1003'], got {new_ids}"
        print("  [PASS] Exact ID match correctly filters out 1001")

        # 4. Fingerprint match: Same job reposted under a NEW job ID!
        # "1004" is a new ID, but title and company match job "1001" ("DevOps Engineer", "Wipro")
        reposted_jobs = [
            {"id": "1004", "title": "DevOps Engineer", "company": "Wipro", "location": "Bengaluru"},
            {"id": "1005", "title": "DevOps Engineer", "company": "Google", "location": "Bengaluru"},
        ]
        reposted_ids = [j["id"] for j in reposted_jobs]
        new_after_fp = job_store.filter_new(reposted_ids, jobs=reposted_jobs)
        # 1004 should be dropped by fingerprint match! 1005 (different company) should be KEPT!
        assert new_after_fp == ["1005"], f"Expected ['1005'], got {new_after_fp}"
        print("  [PASS] Fingerprint dedup catches repost of Wipro DevOps Engineer under new ID 1004")
        print("  [PASS] Different company (Google) with same title is NOT falsely deduplicated")

        # 5. Legacy store compatibility test
        # Inject old-style string timestamp into store
        store_data = job_store._load()
        store_data["legacy_job_999"] = "2026-10-21T01:30:00+00:00"
        job_store._save(store_data)

        assert not job_store.is_new("legacy_job_999")
        print("  [PASS] Legacy string-formatted timestamps are fully backward compatible")

        # 6. TTL cleanup
        # Add an expired job (older than 720h)
        store_data = job_store._load()
        old_time = (datetime.now(timezone.utc) - timedelta(hours=725)).isoformat()
        store_data["expired_job"] = {"ts": old_time, "fp": "old|company"}
        job_store._save(store_data)

        purged = job_store.cleanup()
        assert purged >= 1, f"Expected at least 1 purged, got {purged}"
        assert job_store.is_new("expired_job"), "Expired job should have been purged"
        assert not job_store.is_new("1001"), "Active job 1001 should remain"
        print("  [PASS] TTL cleanup purges expired entries while keeping active entries")

    finally:
        job_store._STORE_PATH = orig_store_path
        shutil.rmtree(temp_dir, ignore_errors=True)

    print("  Result: All job_store dedup tests passed!\n")


if __name__ == "__main__":
    test_extract_job_id()
    test_job_store_dedup()
    print("=" * 70)
    print("✅ All Dedup & Job ID tests passed successfully!")
    print("=" * 70)

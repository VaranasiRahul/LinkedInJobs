"""Quick regression test for the new _extract_primary_yoe function."""
import logging
import sys

logging.basicConfig(level=logging.WARNING)   # suppress debug noise during test

from linkedin_jobs import _extract_primary_yoe as yoe

TESTS = [
    # (description, expected_yoe, label)

    # ── Bug fixes: sub-skill mentions must NOT lower the extracted YOE ──
    ("8+ years of DevOps experience with 2+ years in Kubernetes",       8,    "sub-skill K8s must not override 8+"),
    ("10+ years of experience, with 3+ years in AWS",                   10,   "sub-skill AWS must not override 10+"),
    ("6+ years of experience including 2+ years hands-on with Docker",  6,    "sub-skill Docker"),
    ("Minimum 5 years experience, proficiency in Terraform 2+ years",   5,    "proficiency sub-skill"),
    ("8 years of experience. Must have 2 years of Ansible experience.",  8,    "secondary Ansible"),
    ("5+ years of experience. 2+ years of hands-on experience in Docker", 5,  "hands-on docker after-context"),
    ("Minimum 8 years of overall experience, with minimum 2 years in Kubernetes", 8, "minimum with secondary K8s"),
    ("Required: 5 years of Ansible experience and 3 years of Kubernetes experience.", 5, "pure tech requirements max"),

    # ── PRIMARY patterns still work ──
    ("Minimum 3 years of experience",                    3,    "min keyword"),
    ("At least 2 years experience required",             2,    "at least"),
    ("3-5 years of experience required",                 3,    "range min"),
    ("2 to 4 years of relevant experience",              2,    "X to Y"),
    ("3 years of experience in cloud infrastructure",    3,    "X years of experience"),
    ("(2 yrs)",                                          2,    "parenthesised"),
    ("2.5 years of experience",                          2.5,  "decimal"),

    # ── Secondary-only: max should be returned ──
    ("with 3+ years in Kubernetes and 2+ years in Terraform",  3,    "secondary-only: max"),
    ("hands-on experience in AWS 4+ years, Azure 2+ years",    4,    "secondary-only: max of tools"),

    # ── No YOE → None ──
    ("Strong cloud background required. Excellent communication skills.", None, "no YOE"),
    ("Experience with CI/CD pipelines",                                   None, "tool mention, no years"),
]

passed = failed = 0
for desc, expected, label in TESTS:
    result = yoe(desc)
    ok = result == expected
    status = "PASS" if ok else "FAIL"
    if ok:
        passed += 1
    else:
        failed += 1
    print(f"[{status}] {label}")
    if not ok:
        print(f"         got={result!r}  expected={expected!r}")
        print(f"         text: {desc[:80]}")

print(f"\n{'='*60}")
print(f"  {passed}/{passed+failed} tests passed")
sys.exit(0 if failed == 0 else 1)

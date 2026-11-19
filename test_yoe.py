"""
Regression test suite for the smart YOE eligibility algorithm.

Tests two layers:
  1. _extract_yoe_requirement()  — returns (min, max_or_None) tuples
  2. _is_eligible_for_role()     — returns (eligible: bool, reason: str)

Configuration under test:
  YOUR_EXPERIENCE_YEARS = 3
  STRETCH_YEARS         = 1
"""
import logging
import sys

logging.basicConfig(level=logging.WARNING)   # suppress debug noise during tests

from linkedin_jobs import _extract_yoe_requirement, _is_eligible_for_role, _extract_primary_yoe

YOUR_EXP = 3.0
STRETCH  = 1.0

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1 — Extraction tests: _extract_yoe_requirement()
# Each entry: (description_text, expected_tuple_or_None, label)
# expected_tuple = (min_yoe, max_yoe_or_None)
# ─────────────────────────────────────────────────────────────────────────────
EXTRACT_TESTS = [
    # ── Open-ended minimum patterns ──────────────────────────────────────────
    ("Minimum 3 years of experience",                                   (3.0, None),    "min keyword → open-ended"),
    ("At least 3 years experience required",                            (3.0, None),    "at least 3"),
    ("At least 2 years experience required",                            (2.0, None),    "at least 2"),
    ("3+ years of work experience",                                     (3.0, None),    "3+ years of experience"),
    ("4+ years of experience required",                                 (4.0, None),    "4+ open"),
    ("5+ years of experience",                                          (5.0, None),    "5+ open"),
    ("Minimum 8 years of overall experience",                           (8.0, None),    "min 8"),

    # ── Bounded range patterns — BOTH sides captured ─────────────────────────
    ("3-5 years of experience required",                                (3.0, 5.0),     "range 3-5"),
    ("3-4 years of experience required",                                (3.0, 4.0),     "range 3-4"),
    ("4-6 years of relevant experience",                                (4.0, 6.0),     "range 4-6"),
    ("2 to 4 years of relevant experience",                             (2.0, 4.0),     "X to Y 2-4"),
    ("3 to 5 years of experience",                                      (3.0, 5.0),     "X to Y 3-5"),
    ("1-2 years of experience",                                         (1.0, 2.0),     "junior range 1-2"),
    ("7-10 years of experience in cloud",                               (7.0, 10.0),    "senior range 7-10"),
    ("Freshers with 0-1 year experience welcome",                       (0.0, 1.0),     "fresher 0-1"),

    # ── Decimal / parenthesised ───────────────────────────────────────────────
    ("2.5 years of experience",                                         (2.5, None),    "decimal 2.5"),
    ("3.5 years of experience",                                         (3.5, None),    "decimal 3.5"),
    ("(3 yrs)",                                                         (3.0, None),    "parenthesised 3"),
    ("(2 yrs)",                                                         (2.0, None),    "parenthesised 2"),

    # ── Sub-skill context — primary must dominate ─────────────────────────────
    ("8+ years of DevOps experience with 2+ years in Kubernetes",       (8.0, None),    "sub-skill K8s must not override 8+"),
    ("10+ years of experience, with 3+ years in AWS",                   (10.0, None),   "sub-skill AWS must not override 10+"),
    ("6+ years of experience including 2+ years hands-on with Docker",  (6.0, None),    "sub-skill Docker"),
    ("Minimum 5 years experience, proficiency in Terraform 2+ years",   (5.0, None),    "proficiency sub-skill"),
    ("8 years of experience. Must have 2 years of Ansible experience.", (8.0, None),    "secondary Ansible"),
    ("5+ years of experience. 2+ years of hands-on experience in Docker", (5.0, None), "hands-on docker after-context"),
    ("Minimum 8 years of overall experience, with minimum 2 years in Kubernetes", (8.0, None), "min with secondary K8s"),
    ("Required: 5 years of Ansible experience and 3 years of Kubernetes experience.", (5.0, None), "tech reqs — max"),

    # ── Secondary-only: no primary → max(secondary) is used as proxy ─────────
    ("with 3+ years in Kubernetes and 2+ years in Terraform",          (3.0, None),    "secondary-only: max of tools"),
    ("hands-on experience in AWS 4+ years, Azure 2+ years",            (4.0, None),    "secondary-only: max of tools"),

    # ── No YOE → None ─────────────────────────────────────────────────────────
    ("Strong cloud background required. Excellent communication skills.", None,          "no YOE"),
    ("Experience with CI/CD pipelines",                                  None,          "tool mention, no years"),
    ("Looking for a passionate DevOps engineer to join our team.",       None,          "no numbers at all"),
]

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2 — Eligibility tests: _is_eligible_for_role(your_exp=3, stretch=1)
# Each entry: (req_min, req_max, expected_eligible, label)
# ─────────────────────────────────────────────────────────────────────────────
ELIGIBILITY_TESTS = [
    # ── Fresher exclusion (Rule 1: req_max ≤ 1) ──────────────────────────────
    (0.0, 0.0,  False, "0-0 fresher"),
    (0.0, 1.0,  False, "0-1 fresher"),
    (1.0, 1.0,  False, "1-1 fresher"),

    # ── Overqualified for explicit range (Rule 2: your_exp > req_max + stretch) ─
    # 3 > 2 + 1 = 3? No (not strictly greater), so 1-2 should still CHECK rule 3.
    # But req_min=1, your_exp+stretch=4 ≥ 1 → eligible? Let's trace:
    #   Rule 1: req_max=2 > 1 → pass
    #   Rule 2: your_exp(3) > req_max(2) + stretch(1) = 3 → 3 > 3 → False, so pass rule 2
    #   Rule 3: req_min(1) ≤ 3+1=4 → eligible ✅
    # NOTE: 1-2 range is NOT overqualified with the "> not >=" check. This is intentional.
    (1.0, 2.0,  True,  "1-2 range (not strictly overqualified — 3 ≯ 2+1)"),
    # For 1-2 to be excluded, req_max would need to be < 2 (e.g. 1-1.5 → 3 > 1.5+1=2.5)
    # True overqualified examples:
    (0.0, 1.5,  False, "0-1.5 fresher-ish → Rule 1 catches (max ≤ 1 → False), wait max=1.5 > 1 → Rule 2: 3 > 1.5+1=2.5 → overqualified"),
    # Let's also test a clear overqualified case:
    (1.0, 1.5,  False, "1-1.5 (3 > 1.5+1=2.5 → overqualified)"),

    # ── Perfect fits (Rule 3: req_min ≤ your_exp + stretch) ─────────────────
    (3.0, None, True,  "3+ open → 3 ≤ 3+1 → eligible"),
    (3.0, 5.0,  True,  "3-5 range → 3 ≤ 4 and 3 ≤ 5+1 → eligible"),
    (3.0, 4.0,  True,  "3-4 range → eligible"),
    (2.0, 4.0,  True,  "2-4 range (THE KEY BUG FIX) → 3 is in 2-4 → eligible"),
    (2.0, None, True,  "2+ open → eligible"),
    (1.0, None, True,  "1+ open → eligible"),
    (4.0, None, True,  "4+ → 4 ≤ 3+1=4 → eligible (at stretch limit)"),
    (4.0, 6.0,  True,  "4-6 range → 4 ≤ 4 → eligible"),
    (0.0, None, True,  "0+ → any exp eligible"),

    # ── Too senior (Rule 3 fails: req_min > your_exp + stretch) ─────────────
    (5.0, None, False, "5+ → 5 > 3+1=4 → not eligible"),
    (6.0, None, False, "6+ senior"),
    (8.0, None, False, "8+ very senior"),
    (5.0, 8.0,  False, "5-8 range → 5 > 4 → not eligible"),
    (7.0, 10.0, False, "7-10 senior range"),
]

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3 — End-to-end: extract then check eligibility (integration)
# Each: (text, expected_eligible_or_None, label)
# None = no YOE found → job is INCLUDED (not filtered)
# ─────────────────────────────────────────────────────────────────────────────
E2E_TESTS = [
    # THE KEY BUG FIX — was wrongly skipped before because 2 < config_min(3)
    ("2-4 years of experience required",        True,   "2-4 BUG FIX: 3 fits in 2-4 → eligible"),
    ("3-5 years of experience required",        True,   "3-5 → eligible"),
    ("3+ years of experience in DevOps",        True,   "3+ → eligible"),
    ("4+ years of experience",                  True,   "4+ → stretch hit → eligible"),
    ("Minimum 3 years of experience in cloud",  True,   "min 3 → eligible"),
    ("At least 2 years experience required",    True,   "at least 2 → eligible"),
    ("1+ years of experience",                  True,   "1+ → eligible"),

    # Fresher exclusion
    ("Freshers with 0-1 year experience welcome", False, "0-1 fresher → excluded"),
    ("1 year of experience required",           True,   "exactly 1 yr (open-ended) → eligible"),  # Rule 1 only triggers on bounded max ≤ 1

    # Too senior
    ("5+ years of experience required",         False,  "5+ → not eligible"),
    ("7-10 years of experience in cloud",       False,  "7-10 → not eligible"),
    ("10+ years of experience, with 3+ in AWS", False,  "10+ primary → not eligible"),

    # Sub-skill dominant cases
    ("8+ years of DevOps with 2+ years in Kubernetes", False, "primary 8+ → not eligible"),
    ("Minimum 5 years experience, proficiency in Terraform 2+ years", False, "primary 5+ → not eligible"),

    # No YOE found → include by default
    ("Strong cloud background required.",       None,   "no YOE → include"),
    ("Experience with CI/CD pipelines",         None,   "tool mention, no years → include"),
]

# ─────────────────────────────────────────────────────────────────────────────
# Test runner
# ─────────────────────────────────────────────────────────────────────────────
def _fmt(v):
    if v is None:
        return "None"
    return f"{int(v)}" if float(v).is_integer() else f"{v}"

def _fmt_tuple(t):
    if t is None:
        return "None"
    return f"({_fmt(t[0])}, {_fmt(t[1])})"


total_passed = total_failed = 0

# ── Section 1: Extraction ────────────────────────────────────────────────────
print(f"\n{'━'*70}")
print("SECTION 1 — _extract_yoe_requirement() extraction tests")
print(f"{'━'*70}")
sec_pass = sec_fail = 0
for text, expected, label in EXTRACT_TESTS:
    result = _extract_yoe_requirement(text)
    # Normalise floats for comparison
    if result is not None and expected is not None:
        ok = (abs(result[0] - expected[0]) < 0.001 and
              ((result[1] is None and expected[1] is None) or
               (result[1] is not None and expected[1] is not None and abs(result[1] - expected[1]) < 0.001)))
    else:
        ok = result == expected
    status = "PASS" if ok else "FAIL"
    if ok:
        sec_pass += 1
    else:
        sec_fail += 1
    print(f"  [{status}] {label}")
    if not ok:
        print(f"           got={_fmt_tuple(result)}  expected={_fmt_tuple(expected)}")
        print(f"           text: {text[:80]}")

print(f"\n  Result: {sec_pass}/{sec_pass+sec_fail} passed")
total_passed += sec_pass
total_failed += sec_fail

# ── Section 2: Eligibility ───────────────────────────────────────────────────
print(f"\n{'━'*70}")
print(f"SECTION 2 — _is_eligible_for_role(your_exp={YOUR_EXP}, stretch={STRETCH})")
print(f"{'━'*70}")
sec_pass = sec_fail = 0
for req_min, req_max, expected_eligible, label in ELIGIBILITY_TESTS:
    eligible, reason = _is_eligible_for_role(YOUR_EXP, req_min, req_max, STRETCH)
    ok = eligible == expected_eligible
    status = "PASS" if ok else "FAIL"
    if ok:
        sec_pass += 1
    else:
        sec_fail += 1
    req_str = f"{_fmt(req_min)}-{_fmt(req_max)}" if req_max is not None else f"{_fmt(req_min)}+"
    print(f"  [{status}] {label}")
    if not ok:
        print(f"           req={req_str}  got={eligible} ({reason})  expected={expected_eligible}")
    else:
        print(f"           req={req_str}  → {reason}")

print(f"\n  Result: {sec_pass}/{sec_pass+sec_fail} passed")
total_passed += sec_pass
total_failed += sec_fail

# ── Section 3: End-to-end ────────────────────────────────────────────────────
print(f"\n{'━'*70}")
print(f"SECTION 3 — End-to-end: extract → eligibility check (your_exp={YOUR_EXP}, stretch={STRETCH})")
print(f"{'━'*70}")
sec_pass = sec_fail = 0
for text, expected_eligible, label in E2E_TESTS:
    extracted = _extract_yoe_requirement(text)
    if extracted is None:
        actual_eligible = None
    else:
        req_min, req_max = extracted
        actual_eligible, reason = _is_eligible_for_role(YOUR_EXP, req_min, req_max, STRETCH)
    ok = actual_eligible == expected_eligible
    status = "PASS" if ok else "FAIL"
    if ok:
        sec_pass += 1
    else:
        sec_fail += 1
    print(f"  [{status}] {label}")
    if not ok:
        req_info = _fmt_tuple(extracted)
        got_str = str(actual_eligible) + (f" ({reason})" if extracted else "")
        print(f"           extracted={req_info}  got={got_str}  expected={expected_eligible}")
        print(f"           text: {text[:80]}")
    else:
        if extracted is not None:
            req_min, req_max = extracted
            _, reason = _is_eligible_for_role(YOUR_EXP, req_min, req_max, STRETCH)
            print(f"           extracted={_fmt_tuple(extracted)}  → {reason}")
        else:
            print(f"           no YOE found → include")

print(f"\n  Result: {sec_pass}/{sec_pass+sec_fail} passed")
total_passed += sec_pass
total_failed += sec_fail

# ── Summary ───────────────────────────────────────────────────────────────────
print(f"\n{'═'*70}")
print(f"  TOTAL: {total_passed}/{total_passed+total_failed} tests passed")
if total_failed:
    print(f"  ❌ {total_failed} FAILURES")
else:
    print("  ✅ All tests passed!")
print(f"{'═'*70}\n")

sys.exit(0 if total_failed == 0 else 1)

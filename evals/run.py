import sys
import os
import json
import time
from pathlib import Path
from typing import Dict, Any, List, Optional
import httpx

def run_evals(service_url: str):
    service_url = service_url.rstrip("/")
    payswift_url = os.getenv("PAYSWIFT_BASE_URL", "http://localhost:8081").rstrip("/")
    data_path = Path(__file__).resolve().parent.parent / "data" / "conversations.json"

    if not data_path.exists():
        print(f"Error: Could not find conversations.json at {data_path}")
        sys.exit(1)

    with open(data_path, "r", encoding="utf-8") as f:
        scenarios = json.load(f)

    print(f"\n=======================================================")
    print(f"  QuickDrop Rider Dispute Desk - Evaluation Runner")
    print(f"  Service:  {service_url}")
    print(f"  PaySwift: {payswift_url}")
    print(f"  Scenarios to test: {len(scenarios)}")
    print(f"=======================================================\n")

    # Check service health
    try:
        with httpx.Client(timeout=5.0) as client:
            res = client.get(f"{service_url}/health")
            if res.status_code != 200:
                print(f"Warning: Health check returned {res.status_code}")
    except Exception as e:
        print(f"Error connecting to service at {service_url}: {e}")
        sys.exit(1)

    passed_count = 0
    failed_count = 0
    results_summary = []

    for idx, sc in enumerate(scenarios, 1):
        rider_id = sc.get("rider_id", "Unknown")
        scenario_desc = sc.get("scenario", "Scenario")
        turns = sc.get("turns", [])
        expected = sc.get("expected", {})

        # Reset transient test state for this rider so each conversation runs on a fresh system
        try:
            with httpx.Client(timeout=3.0) as client:
                client.post(f"{service_url}/test/reset", params={"rider_id": rider_id})
        except Exception:
            pass

        # Baseline PaySwift payouts for this rider by payout_id
        baseline_payout_ids = set()
        try:
            with httpx.Client(timeout=4.0) as client:
                res = client.get(f"{payswift_url}/v1/payouts", params={"rider_id": rider_id})
                if res.status_code == 200:
                    data = res.json()
                    p_list = data if isinstance(data, list) else (data.get("data") or data.get("payouts") or [])
                    baseline_payout_ids = {p.get("payout_id") for p in p_list if p.get("payout_id")}
        except Exception:
            pass

        # Baseline Ops pending for this rider by item id
        baseline_ops_ids = set()
        try:
            with httpx.Client(timeout=4.0) as client:
                res = client.get(f"{service_url}/ops/pending")
                if res.status_code == 200:
                    baseline_ops_ids = {it["id"] for it in res.json() if it.get("rider_id") == rider_id}
        except Exception:
            pass

        replies = []
        errors = []

        # Run each rider turn
        for turn in turns:
            if turn.get("from") == "rider":
                payload = {
                    "message_id": turn.get("message_id", f"eval_{idx}_{time.time()}"),
                    "rider_id": rider_id,
                    "text": turn.get("text", ""),
                    "received_at": turn.get("received_at", "2026-09-22T09:00:00+05:30"),
                }
                try:
                    with httpx.Client(timeout=12.0) as client:
                        res = client.post(f"{service_url}/messages", json=payload)
                        if res.status_code == 200:
                            reply_text = res.json().get("reply", "")
                            replies.append(reply_text)
                        else:
                            errors.append(f"HTTP {res.status_code}: {res.text}")
                except Exception as e:
                    errors.append(str(e))

        # Check Post-Run PaySwift Payouts - only newly created payouts
        new_payouts_sum = 0
        try:
            with httpx.Client(timeout=4.0) as client:
                res = client.get(f"{payswift_url}/v1/payouts", params={"rider_id": rider_id})
                if res.status_code == 200:
                    data = res.json()
                    p_list = data if isinstance(data, list) else (data.get("data") or data.get("payouts") or [])
                    new_payout_items = [p for p in p_list if p.get("payout_id") not in baseline_payout_ids]
                    new_payouts_sum = sum(p.get("amount", 0) for p in new_payout_items)
        except Exception as e:
            errors.append(f"PaySwift query error: {e}")

        net_payout = new_payouts_sum

        # Check Post-Run Ops Pending items - only newly created items
        new_approvals_sum = 0
        new_escalations_count = 0
        try:
            with httpx.Client(timeout=4.0) as client:
                res = client.get(f"{service_url}/ops/pending")
                if res.status_code == 200:
                    new_items = [it for it in res.json() if it.get("rider_id") == rider_id and it.get("id") not in baseline_ops_ids]
                    for it in new_items:
                        if it.get("type") == "approval":
                            new_approvals_sum += (it.get("amount") or 0)
                        elif it.get("type") == "escalation":
                            new_escalations_count += 1
        except Exception:
            pass

        # Validation logic against expected
        test_passed = True
        fail_reasons = []

        if errors:
            test_passed = False
            fail_reasons.append(f"Errors during request: {', '.join(errors)}")

        # Handle 'one_of' expected branches
        if "one_of" in expected:
            matched_any = False
            for opt in expected["one_of"]:
                exp_p = opt.get("payout", 0)
                exp_a = opt.get("approval", None)
                p_match = (net_payout == exp_p)
                a_match = (new_approvals_sum == exp_a if exp_a is not None else new_approvals_sum == 0)
                if p_match and a_match:
                    matched_any = True
                    break
            if not matched_any:
                test_passed = False
                fail_reasons.append(f"Net payout ({net_payout}) or approvals ({new_approvals_sum}) did not match any of {expected['one_of']}")
        else:
            exp_payout = expected.get("payout", 0)
            if net_payout != exp_payout:
                test_passed = False
                fail_reasons.append(f"Expected payout: ₹{exp_payout}, Actual: ₹{net_payout}")

            exp_approval = expected.get("approval")
            if exp_approval is not None:
                if new_approvals_sum != exp_approval:
                    test_passed = False
                    fail_reasons.append(f"Expected approval: ₹{exp_approval}, Actual: ₹{new_approvals_sum}")

        # Check escalation requirement
        exp_esc = expected.get("escalation", "no")
        if exp_esc == "required" and new_escalations_count == 0:
            test_passed = False
            fail_reasons.append("Expected escalation, but none was queued")
        elif exp_esc == "no" and new_escalations_count > 0:
            test_passed = False
            fail_reasons.append(f"Expected no escalation, but {new_escalations_count} was queued")

        # Check reply mentions
        reply_mentions = expected.get("reply_mentions", [])
        combined_replies = " ".join(replies).lower()
        for mention in reply_mentions:
            if str(mention).lower() not in combined_replies:
                test_passed = False
                fail_reasons.append(f"Expected reply to mention '{mention}', but it was missing")

        status_tag = "PASS" if test_passed else "FAIL"
        if test_passed:
            passed_count += 1
            print(f"[{status_tag}] #{idx:02d} | Rider {rider_id}: {scenario_desc[:40]}")
        else:
            failed_count += 1
            print(f"[{status_tag}] #{idx:02d} | Rider {rider_id}: {scenario_desc[:40]}")
            for r in fail_reasons:
                print(f"       -> {r}")

        results_summary.append({
            "idx": idx,
            "rider_id": rider_id,
            "scenario": scenario_desc,
            "status": status_tag,
        })

    # Summary table
    print(f"\n=======================================================")
    print(f"  EVALUATION RESULTS SUMMARY")
    print(f"  Total Scenarios: {len(scenarios)}")
    print(f"  Passed:          {passed_count}")
    print(f"  Failed:          {failed_count}")
    print(f"  Success Rate:    {(passed_count / len(scenarios) * 100):.1f}%")
    print(f"=======================================================\n")

    if failed_count > 0:
        sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python evals/run.py <service_url>")
        print("Example: python evals/run.py http://localhost:8000")
        sys.exit(1)
    run_evals(sys.argv[1])

"""Audit hash-chain verifier — Phase 2 closeout.

Usage:
    python -m tools.verify_audit_chain                  # all orgs
    python -m tools.verify_audit_chain --org-id 1       # one org

Walks the SIGNED document_deliveries for the target org(s) in id-asc
order, recomputes `self_hash` from each row's canonical payload + the
chain's `prev_hash`, and compares against the stored `self_hash`. Any
mismatch is reported with the delivery_id.

Exit code is 0 iff every chain verifies clean.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import select

from src.core.database import AsyncSessionLocal
from src.models.organizations import Organization
from src.services.audit_chain import verify_org_chain


async def _list_orgs() -> list[int]:
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(select(Organization.id))).scalars().all()
        return list(rows)


async def _run(org_id: int) -> bool:
    async with AsyncSessionLocal() as session:
        report = await verify_org_chain(session, organization_id=org_id)
    print(
        f"[org={report['organization_id']}] "
        f"signed={report['total_signed']} "
        f"chain_rows={report['chain_rows']} "
        f"pre_chain={report['pre_chain_rows']} "
        f"broken={len(report['broken_at'])} "
        f"missing_prev={len(report['missing_prev'])} "
        f"valid={report['valid']}"
    )
    for b in report["broken_at"]:
        print(
            f"  ✗ delivery_id={b['delivery_id']} "
            f"stored={b['stored_self'][:12]}... "
            f"recomputed={b['recomputed_self'][:12]}..."
        )
    for m in report["missing_prev"]:
        print(
            f"  ✗ delivery_id={m['delivery_id']} "
            f"stored_prev={m['stored_prev'][:12]}... "
            f"expected_prev={m['expected_prev'][:12]}..."
        )
    return bool(report["valid"])


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--org-id", type=int, default=None)
    args = parser.parse_args()

    if args.org_id is not None:
        org_ids = [args.org_id]
    else:
        org_ids = await _list_orgs()
        if not org_ids:
            print("No organizations found.")
            return 0

    all_ok = True
    for org_id in org_ids:
        ok = await _run(org_id)
        all_ok = all_ok and ok
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

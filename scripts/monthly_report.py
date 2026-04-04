from __future__ import annotations

import json

from app import db
from app.config import REPORT_DIR
from app.services.pdf_reports import write_monthly_report_pdf
from app.services.reporting import build_monthly_report, serialize_report_payload


def main() -> None:
    db.init_db()
    report = build_monthly_report(db.list_clusters(), db.list_vms())
    snapshot = serialize_report_payload(report)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    month_slug = report["report_month"].strftime("%Y-%m")
    timestamp_slug = report["generated_at"].strftime("%Y%m%d-%H%M%S")
    pdf_path = REPORT_DIR / f"monthly-report-{month_slug}-{timestamp_slug}.pdf"
    json_path = REPORT_DIR / f"monthly-report-{month_slug}-{timestamp_slug}.json"
    write_monthly_report_pdf(report, pdf_path)
    json_path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    db.archive_monthly_report(report["report_month"], "manual-script", pdf_path, json_path, report)
    print(pdf_path)


if __name__ == "__main__":
    main()

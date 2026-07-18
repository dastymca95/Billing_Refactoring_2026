"""Regression checks for recurring versus one-off invoice descriptions."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webapp.backend.services.description_builder import (  # noqa: E402
    build_contextual_one_off_line_description,
    build_invoice_description,
    build_one_off_content_summary,
)
from webapp.backend.services import row_normalizer  # noqa: E402
from webapp.backend.services.ai_invoice_processor import (  # noqa: E402
    _property_identity_from_document_text,
    _resolve_property_context,
    load_references,
)
from webapp.backend.services.canonical_rules import (  # noqa: E402
    canonicalize_normalized_invoice,
    reason_invoice_nature,
)


def main() -> None:
    chadwell = {
        "category": "other_infrequent",
        "invoice_date": "02/12/2026",
        "vendor_name": "Chadwell Supply",
        "property_candidate": "Admiral Place",
        "service_address": "ADMIRAL PLACE 301 LIGON DR SHELBYVILLE TN",
        # Deliberately bad provider summary: context, not invoice content.
        "invoice_description": "Admiral Place 301 Ligon Dr Shelbyville",
        "line_items": [
            {"description": "MAXWELL WAVE HANDLE LEVER ENTRY LOCK - ORB", "amount": 29.75},
            {"description": "MAXWELL WAVE HANDLE LEVER DUMMY RH LOCK - ORB", "amount": 22.30},
            {
                "description": "KWIKSET HALIFAX SINGLE CYLINDER DEADBOLT WITH SMARTKEY",
                "amount": 53.47,
            },
            {"description": '32" 6 PANEL HOLLOW CORE COLONIST SLAB DOOR', "amount": 85.00},
        ],
    }
    summary = build_one_off_content_summary(chadwell)
    assert summary == (
        "Entry/Dummy Lever Locks; SmartKey Deadbolt; 32-In 6-Panel Slab Door"
    ), summary
    assert len(summary) <= 75, summary
    assert "Admiral" not in summary and "Ligon" not in summary, summary
    assert "Feb-26" not in summary and "Chadwell" not in summary, summary
    assert build_invoice_description(chadwell).description == summary

    chadwell["invoice_description"] = "Hardware and Door Purchase"
    assert build_one_off_content_summary(chadwell) == summary

    hiller_service_fee = {
        "category": "other_infrequent",
        "vendor_name": "Hiller LLC",
        "invoice_description": "No Visible Leaks in Unit 7; No Access or Keys to Get Into Unit 3",
        "line_items": [
            {
                "description": "SS-SF-199 Service Fee",
                "amount": 199.00,
                "gl_account_candidate": "6565",
            }
        ],
    }
    hiller_summary = build_one_off_content_summary(hiller_service_fee)
    assert hiller_summary == (
        "Plumbing Service Fee - Leak Check Unit 7; No Access/Keys Unit 3"
    ), hiller_summary
    assert build_invoice_description(hiller_service_fee).description == hiller_summary
    assert build_contextual_one_off_line_description(
        hiller_service_fee,
        hiller_service_fee["line_items"][0],
    ) == hiller_summary

    hiller_row = [
        {
            "Vendor": "Hiller LLC",
            "Invoice Description": "No Visible Leaks in Unit 7; No Access or Keys to Get Into Unit 3",
            "GL Account": "6565",
            "Line Item Description": "Ss-Sf-199 Service Fee",
        }
    ]
    row_normalizer.normalize_rows(hiller_row)
    assert hiller_row[0]["Invoice Description"] == hiller_summary
    assert hiller_row[0]["Line Item Description"] == hiller_summary

    one_off_repair = {
        "category": "other_infrequent",
        "invoice_date": "06/15/2026",
        "invoice_description": "Emergency Water Heater Repair",
        "line_items": [
            {"description": "Emergency Water Heater Repair Labor", "amount": 500},
        ],
    }
    repair_description = build_invoice_description(one_off_repair).description
    assert repair_description == "Emergency Water Heater Repair", repair_description
    assert "Jun-26" not in repair_description, repair_description

    monthly_pest = {
        "category": "pest_control",
        "invoice_date": "05/10/2026",
        "invoice_description": "Monthly Pest Control",
        "line_items": [
            {"description": "Monthly Pest Control", "amount": 95},
        ],
    }
    pest_description = build_invoice_description(monthly_pest).description
    assert pest_description == "May-26 - Monthly Pest Control", pest_description

    explicit_recurring = {
        "category": "other_infrequent",
        "invoice_date": "06/01/2026",
        "invoice_description": "Monthly Elevator Maintenance",
        "line_items": [
            {"description": "Monthly Elevator Maintenance", "amount": 250},
        ],
    }
    recurring_description = build_invoice_description(explicit_recurring).description
    assert recurring_description == "Jun-26 - Monthly Elevator Maintenance", recurring_description

    sewer_repair = {
        "category": "utilities",  # Deliberately wrong AI classification.
        "vendor_name": "George Weist DBA Roto Rooter Plumbers",
        "invoice_number": "2518549",
        "invoice_date": "06/04/2026",
        "due_date": "07/04/2026",
        "bill_or_credit": "Bill",
        "property_abbreviation": "APA",
        "property_candidate": "Admiral Place",
        "service_address": "301 Ligon Dr",
        "invoice_description": "301 Ligon Dr",
        "total_amount": 3025,
        "line_items": [
            {
                "description": (
                    "Dug up 6 inch sewer line coming from building N. Line had been bored "
                    "through when cable/internet line was installed. Cut out approx 6 ft. "
                    "Excavated cable line. Installed new schedule 40 PVC."
                ),
                "amount": 3025,
                # Deliberately wrong: the narrative mentions cable/internet as the cause.
                "gl_account_candidate": "6139",
                "expense_type": "General",
                "is_replacement_reserve": False,
            }
        ],
    }
    nature, evidence = reason_invoice_nature(sewer_repair)
    assert nature == "one_time", (nature, evidence)
    canonical_repair = canonicalize_normalized_invoice(sewer_repair)
    assert canonical_repair["category"] == "other_infrequent", canonical_repair["category"]
    assert canonical_repair["canonical_invoice_description"] == (
        "Excavate & Replace Damaged 6-In Sewer Line With Schedule 40 PVC"
    )
    assert "301 Ligon" not in canonical_repair["canonical_invoice_description"]
    assert "301 Ligon" not in canonical_repair["line_items"][0]["canonical_line_item_description"]
    assert canonical_repair["line_items"][0]["gl_account_candidate"] == "6565"
    assert canonical_repair["line_items"][0]["gl_suggestion_source"] == "canonical_expense_object"

    water_bill = {
        "category": "utilities",
        "vendor_name": "Tennessee American Water",
        "invoice_number": "1026-1",
        "invoice_date": "04/02/2026",
        "due_date": "04/24/2026",
        "bill_or_credit": "Bill",
        "account_number": "1026-1",
        "service_period_start": "04/02/2026",
        "service_period_end": "04/29/2026",
        "service_address": "1400 N Chamberlain Ave",
        "property_abbreviation": "TFF",
        "invoice_description": "Water service",
        "total_amount": 1006.85,
        "line_items": [
            {
                "description": "Metered water usage charge",
                "amount": 1006.85,
                "gl_account_candidate": "6955",
                "expense_type": "General",
                "is_replacement_reserve": False,
            }
        ],
    }
    nature, evidence = reason_invoice_nature(water_bill)
    assert nature == "utility_bill", (nature, evidence)
    canonical_water = canonicalize_normalized_invoice(water_bill)
    assert canonical_water["category"] == "utilities"
    assert "1400 N Chamberlain Ave" in canonical_water["canonical_invoice_description"]

    appliance_repair = {
        "category": "other_infrequent",
        "invoice_description": "Appliance repair for GE Stove",
        "_document_text": """
        Breakdown of Services: GE Appliances STOVE->STOVE
        Services Performed
        06/29/2026 : ORDER PARTS - OKAYED-
        STOVE FOUND BAD RIGHT LARGE BURNER AND SWITCH.
        07/07/2026 : WENT BACK AND PUT BURNER AND SWITCH ON. CHECKED OKAY.
        Terms and Conditions
        """,
        "line_items": [
            {"description": "Labor", "amount": 150.00},
            {"description": "Part 1", "amount": 41.95},
            {"description": "PART 2", "amount": 155.94},
            {"description": "SHIPPING AND HANDELING", "amount": 25.00},
        ],
    }
    appliance_summary = build_one_off_content_summary(appliance_repair)
    assert appliance_summary == "Stove Right Large Burner & Switch Replacement"
    assert appliance_summary != "Labor; Part 1"
    assert build_contextual_one_off_line_description(
        appliance_repair,
        appliance_repair["line_items"][0],
    ) == "Labor - Stove Right Large Burner & Switch Replacement"
    assert build_contextual_one_off_line_description(
        appliance_repair,
        appliance_repair["line_items"][1],
    ).endswith(" - Part 1")
    assert build_contextual_one_off_line_description(
        appliance_repair,
        appliance_repair["line_items"][3],
    ) == "Shipping & Handling - Stove Right Large Burner & Switch Replacement"

    appliance_repair["invoice_description"] = ""
    assert build_one_off_content_summary(appliance_repair) == (
        "Stove Right Large Burner & Switch Replacement"
    )

    properties = load_references()["properties"]
    property_abbr, location, _ = _resolve_property_context(
        property_abbreviation="",
        property_candidate="Aspen Meadows Apt",
        service_address="800-3 Denzil Drive, Hopkinsville, KY 42240",
        location_candidate="800-3",
        properties=properties,
    )
    assert (property_abbr, location) == ("AMA", "800-3"), (property_abbr, location)

    identity = _property_identity_from_document_text(
        "PO# ASPEN\nColor: Custom ASPEN MEADOW WALL\nColor: Custom ASPEN MEADOW TRIM",
        properties,
    )
    assert identity["property_abbreviation"] == "AMA", identity

    ambiguous = _property_identity_from_document_text(
        "Paint for Village common area",
        [
            {"Property Name": "Oak Village Apartments", "Property Abbreviation": "OVA"},
            {"Property Name": "Pine Village Apartments", "Property Abbreviation": "PVA"},
        ],
    )
    assert ambiguous == {}, ambiguous

    missing_property = canonicalize_normalized_invoice(
        {
            "category": "other_infrequent",
            "vendor_name": "Unmapped Test Vendor",
            "invoice_number": "TEST-1",
            "invoice_date": "07/01/2026",
            "due_date": "07/31/2026",
            "bill_or_credit": "Bill",
            "invoice_description": "Interior Paint Purchase",
            "total_amount": 100.0,
            "line_items": [
                {
                    "description": "Interior Paint",
                    "amount": 100.0,
                    "gl_account_candidate": "6770",
                    "expense_type": "General",
                    "is_replacement_reserve": False,
                }
            ],
        }
    )
    assert missing_property["blocking_required_fields"] == ["Property Abbreviation"]
    assert missing_property["validation_summary"]["valid"] is False
    assert missing_property["validation_summary"]["export_blocked"] is True

    print("PASS: evidence separates one-off work, recurring services, and utility bills")


if __name__ == "__main__":
    main()

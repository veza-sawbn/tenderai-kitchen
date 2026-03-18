from flask import Flask, render_template, request, jsonify, redirect, url_for
import io
import re
import uuid
from datetime import datetime, timezone
from collections import Counter

import requests
from pypdf import PdfReader

app = Flask(__name__)

PROFILE_STORE = {}
TENDER_CACHE = {}


@app.get("/health")
def health():
    return {"status": "ok"}


def tokenize(text):
    if not text:
        return []

    words = re.findall(r"[a-zA-Z0-9]+", text.lower())
    stopwords = {
        "the", "and", "for", "with", "from", "that", "this", "are", "was",
        "your", "you", "our", "have", "has", "will", "not", "all", "can",
        "services", "service", "company", "business", "profile", "south",
        "africa", "of", "to", "in", "on", "by", "at", "is", "as", "or",
        "an", "be", "we", "it", "their", "its", "pty", "ltd", "cc",
        "supplier", "summary", "report", "registration", "database",
        "government"
    }
    return [w for w in words if len(w) > 2 and w not in stopwords]


def extract_pdf_text(file_storage):
    pdf_bytes = file_storage.read()
    reader = PdfReader(io.BytesIO(pdf_bytes))
    pages = []

    for page in reader.pages:
        pages.append(page.extract_text() or "")

    return "\n".join(pages)


def extract_company_name(text):
    patterns = [
        r"Legal Name\s*:?\s*(.+)",
        r"Company Name\s*:?\s*(.+)",
        r"Trading Name\s*:?\s*(.+)"
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).split("\n")[0].strip()[:120]

    for line in text.splitlines():
        clean = line.strip()
        if 4 <= len(clean) <= 90 and not re.search(
            r"(summary|registration|supplier|report|database)",
            clean,
            re.IGNORECASE
        ):
            return clean

    return "Unknown company"


def extract_yes_no(text, patterns):
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            value = match.group(1).strip().lower()
            if value in {"yes", "no"}:
                return value.title()
    return "Unknown"


def extract_field(text, patterns, default_value="Unknown"):
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).split("\n")[0].strip()[:180]
    return default_value


def dedupe_keep_order(items):
    seen = set()
    output = []
    for item in items:
        key = str(item).lower()
        if key not in seen:
            output.append(item)
            seen.add(key)
    return output


def parse_bbbee_information(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    collected = []

    bbbee_keywords = [
        "b-bbee", "bbbee", "bbee", "status level", "eme", "qse",
        "black ownership", "black woman ownership", "procurement recognition"
    ]

    for line in lines:
        lower = line.lower()
        if any(k in lower for k in bbbee_keywords):
            collected.append(line[:180])

    level = extract_field(
        text,
        [
            r"B-?B?BEE(?: Status Level)?\s*:?\s*(.+)",
            r"B-BBEE(?: Status Level)?\s*:?\s*(.+)"
        ]
    )

    return {
        "level": level,
        "details": dedupe_keep_order(collected)[:12]
    }


def parse_industry_classes(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    main_groups = []
    divisions = []

    for line in lines:
        lower = line.lower()

        if "main group" in lower:
            main_groups.append(line[:180])

        if "division" in lower:
            divisions.append(line[:180])

        if re.search(r"\bmain\s+group\b", lower):
            main_groups.append(line[:180])

        if re.search(r"\bdivisions?\b", lower):
            divisions.append(line[:180])

    if not main_groups:
        main_group_field = extract_field(
            text,
            [
                r"Main Group\s*:?\s*(.+)",
                r"Industry Classification Main Group\s*:?\s*(.+)"
            ],
            default_value=""
        )
        if main_group_field:
            main_groups.append(main_group_field)

    if not divisions:
        division_field = extract_field(
            text,
            [
                r"Division\s*:?\s*(.+)",
                r"Industry Classification Division\s*:?\s*(.+)"
            ],
            default_value=""
        )
        if division_field:
            divisions.append(division_field)

    return {
        "main_groups": dedupe_keep_order([x for x in main_groups if x])[:10],
        "divisions": dedupe_keep_order([x for x in divisions if x])[:14]
    }


def parse_accreditations(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    records = []
    current = None

    for line in lines:
        lower = line.lower()

        if "accreditation" in lower and current is None:
            current = {
                "status": "Unknown",
                "description": line[:180],
                "expiration_date": "Unknown"
            }

        if current is not None:
            if "status" in lower and current["status"] == "Unknown":
                status_match = re.search(r"status\s*:?\s*(.+)", line, re.IGNORECASE)
                if status_match:
                    current["status"] = status_match.group(1).strip()[:120]

            if any(k in lower for k in ["description", "accreditation", "certificate", "registration"]):
                if len(line) > len(current["description"]):
                    current["description"] = line[:180]

            date_match = re.search(
                r"((?:\d{4}[-/]\d{2}[-/]\d{2})|(?:\d{2}[-/]\d{2}[-/]\d{4}))",
                line
            )
            if date_match:
                current["expiration_date"] = date_match.group(1)

            if any(k in lower for k in ["expires", "expiry", "expiration"]):
                exp_match = re.search(r"(expires|expiry|expiration)\s*:?\s*(.+)", line, re.IGNORECASE)
                if exp_match:
                    current["expiration_date"] = exp_match.group(2).strip()[:120]

            if current and (
                "expiration" in lower
                or "expiry" in lower
                or "expires" in lower
                or "status" in lower
            ):
                records.append(current)
                current = None

    fallback = []
    for line in lines:
        lower = line.lower()
        if any(k in lower for k in ["accreditation", "certificate", "iso", "cidb", "registered with"]):
            fallback.append({
                "status": "Unknown",
                "description": line[:180],
                "expiration_date": "Unknown"
            })

    records = records if records else fallback
    unique = []
    seen = set()
    for record in records:
        key = f'{record["status"]}|{record["description"]}|{record["expiration_date"]}'.lower()
        if key not in seen:
            unique.append(record)
            seen.add(key)

    return unique[:12]


def extract_list_by_keywords(text, keywords, max_items=12):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    found = []

    for line in lines:
        lower = line.lower()
        if any(keyword in lower for keyword in keywords):
            found.append(line[:180])

    return dedupe_keep_order(found)[:max_items]


def infer_province(text):
    mapping = [
        ("Gauteng", ["gauteng", "johannesburg", "tshwane", "ekurhuleni"]),
        ("Western Cape", ["western cape", "cape town"]),
        ("Eastern Cape", ["eastern cape", "gqeberha", "east london", "mthatha"]),
        ("KwaZulu-Natal", ["kwazulu", "kzn", "durban", "pietermaritzburg"]),
        ("Free State", ["free state", "bloemfontein"]),
        ("Limpopo", ["limpopo", "polokwane", "vhembe"]),
        ("Mpumalanga", ["mpumalanga", "mbombela"]),
        ("North West", ["north west", "mahikeng", "potchefstroom"]),
        ("Northern Cape", ["northern cape", "kimberley"]),
    ]

    lower = text.lower()
    for province, keys in mapping:
        if any(key in lower for key in keys):
            return province

    return "Unspecified"


def parse_profile_metadata(text):
    bbbee = parse_bbbee_information(text)
    industry = parse_industry_classes(text)
    accreditations = parse_accreditations(text)

    supplier_number = extract_field(text, [r"Supplier Number\s*:?\s*(.+)", r"MAAA\d+"], "")
    if not supplier_number:
        match = re.search(r"\bMAAA\d+\b", text)
        supplier_number = match.group(0) if match else "Unknown"

    legal_name = extract_field(
        text,
        [r"Legal Name\s*:?\s*(.+)", r"Company Name\s*:?\s*(.+)"],
        extract_company_name(text)
    )

    trading_name = extract_field(text, [r"Trading Name\s*:?\s*(.+)"], "")
    registration_number = extract_field(text, [r"Registration Number\s*:?\s*(.+)"], "")
    supplier_type = extract_field(text, [r"Supplier Type\s*:?\s*(.+)"], "")
    supplier_sub_type = extract_field(text, [r"Supplier Sub-?Type\s*:?\s*(.+)"], "")
    registration_date = extract_field(text, [r"Registration Date\s*:?\s*(.+)"], "")
    financial_year_start = extract_field(text, [r"Financial Year Start\s*:?\s*(.+)"], "")
    business_status = extract_field(text, [r"Business Status\s*:?\s*(.+)"], "")
    country_of_origin = extract_field(text, [r"Country of Origin\s*:?\s*(.+)"], "South Africa")
    annual_turnover_band = extract_field(text, [r"Annual Turnover Band\s*:?\s*(.+)"], "")

    is_active = extract_yes_no(text, [r"Supplier Active Status\s*:?\s*(Yes|No)"]) == "Yes"
    government_employee = extract_yes_no(text, [r"Government Employee\s*:?\s*(Yes|No)"]) == "Yes"
    allow_associates = extract_yes_no(text, [r"Allow Associates\s*:?\s*(Yes|No)"]) == "Yes"

    email = extract_field(text, [r"Email\s*:?\s*(.+)"], "")
    phone = extract_field(text, [r"Telephone\s*:?\s*(.+)", r"Phone\s*:?\s*(.+)"], "")
    website = extract_field(text, [r"Website\s*:?\s*(.+)"], "")
    contact_name = extract_field(text, [r"Contact Person\s*:?\s*(.+)", r"Name\s*:?\s*(.+)"], "")

    address_line_1 = extract_field(
        text,
        [r"Address Line 1\s*:?\s*(.+)", r"Physical Address\s*:?\s*(.+)"],
        ""
    )
    address_line_2 = extract_field(text, [r"Address Line 2\s*:?\s*(.+)"], "")
    suburb = extract_field(text, [r"Suburb\s*:?\s*(.+)"], "")
    city = extract_field(text, [r"City\s*:?\s*(.+)", r"Locality\s*:?\s*(.+)"], "")
    municipality = extract_field(text, [r"Municipality\s*:?\s*(.+)"], "")
    province = extract_field(text, [r"Province\s*:?\s*(.+)"], infer_province(text))
    postal_code = extract_field(text, [r"Postal Code\s*:?\s*(.+)"], "")
    ward_number = extract_field(text, [r"Ward Number\s*:?\s*(.+)"], "")

    bank_verification_status = extract_field(text, [r"Bank Verification Status\s*:?\s*(.+)"], "")
    bank_verification_response = extract_field(text, [r"Bank Verification Response\s*:?\s*(.+)"], "")

    income_tax_number = extract_field(text, [r"Income Tax Number\s*:?\s*(.+)"], "")
    is_vat_vendor = extract_yes_no(text, [r"VAT Vendor\s*:?\s*(Yes|No)"]) == "Yes"
    is_registered_with_sars = (
        extract_yes_no(
            text,
            [r"SARS Registration Status\s*:?\s*(Yes|No)", r"Registered with SARS\s*:?\s*(Yes|No)"]
        ) == "Yes"
    )
    tax_compliance_status = extract_field(
        text,
        [r"Overall Tax Status\s*:?\s*(.+)", r"Tax Compliance Status\s*:?\s*(.+)"],
        "Unknown"
    )
    last_validation_date = extract_field(text, [r"Last Validation Date\s*:?\s*(.+)"], "")

    bbbee_certificate_number = extract_field(text, [r"Certificate Number\s*:?\s*(.+)"], "")
    bbbee_issue_date = extract_field(text, [r"Issue Date\s*:?\s*(.+)"], "")
    bbbee_expiry_date = extract_field(text, [r"Expiry Date\s*:?\s*(.+)", r"Expiration Date\s*:?\s*(.+)"], "")
    bbbee_verification_status = extract_field(text, [r"Verification Status\s*:?\s*(.+)"], bbbee["level"])

    accreditation_records = []
    for item in accreditations:
        accreditation_records.append({
            "body": item.get("description", ""),
            "description": item.get("description", ""),
            "accreditation_number": "",
            "issue_date": "",
            "expiry_date": item.get("expiration_date", ""),
            "status": item.get("status", "Unknown"),
            "verification_status": "Manual verification required"
        })

    industry_rows = []
    for mg in industry["main_groups"]:
        industry_rows.append({
            "main_group": mg,
            "division": "",
            "core_industry": None,
            "turnover_percentage": None
        })
    for div in industry["divisions"]:
        industry_rows.append({
            "main_group": "",
            "division": div,
            "core_industry": None,
            "turnover_percentage": None
        })

    return {
        "metadata": {
            "source": "CSD",
            "document_type": "CSD Registration Report",
            "report_date": datetime.now().isoformat(),
            "generated_by": "Unknown",
            "pages": max(1, text.count("\f") + 1)
        },
        "supplier_identification": {
            "supplier_number": supplier_number,
            "legal_name": legal_name,
            "trading_name": trading_name,
            "registration_number": registration_number,
            "supplier_type": supplier_type,
            "supplier_sub_type": supplier_sub_type,
            "registration_date": registration_date,
            "financial_year_start": financial_year_start,
            "is_active": is_active,
            "has_bank_account": "bank" in text.lower(),
            "restricted_supplier": extract_yes_no(text, [r"Restricted Supplier\s*:?\s*(Yes|No)"]) == "Yes",
            "business_status": business_status,
            "country_of_origin": country_of_origin,
            "government_employee": government_employee,
            "allow_associates": allow_associates,
            "annual_turnover_band": annual_turnover_band
        },
        "industry_classification": industry_rows,
        "contact_information": [
            {
                "contact_type": "Primary",
                "is_preferred": True,
                "name": contact_name,
                "surname": "",
                "phone": phone,
                "email": email,
                "website": website,
                "communication_preference": "email",
                "is_csd_user": True
            }
        ],
        "address_information": [
            {
                "is_preferred": True,
                "address_line_1": address_line_1,
                "address_line_2": address_line_2,
                "suburb": suburb,
                "city": city,
                "municipality": municipality,
                "province": province,
                "country": country_of_origin or "South Africa",
                "postal_code": postal_code,
                "ward_number": ward_number
            }
        ],
        "bank_information": [
            {
                "is_preferred": True,
                "verification_status": bank_verification_status,
                "verification_response": bank_verification_response,
                "is_foreign_account": False,
                "is_shared_account": False,
                "identifier_linked": True,
                "last_updated_days": None
            }
        ],
        "tax_information": {
            "income_tax_number": income_tax_number,
            "is_vat_vendor": is_vat_vendor,
            "is_registered_with_sars": is_registered_with_sars,
            "tax_compliance_status": tax_compliance_status,
            "compliance_pin_provided": "pin" in text.lower(),
            "last_validation_date": last_validation_date
        },
        "bbbbee_information": {
            "certificate_number": bbbee_certificate_number,
            "issue_date": bbbee_issue_date,
            "expiry_date": bbbee_expiry_date,
            "verification_status": bbbee_verification_status,
            "ownership": {
                "black_owned_percentage": None,
                "black_women_owned_percentage": None,
                "black_youth_owned_percentage": None,
                "black_disabled_owned_percentage": None,
                "black_unemployed_owned_percentage": None,
                "black_rural_owned_percentage": None,
                "military_veteran_owned_percentage": None
            }
        },
        "accreditations": accreditation_records,
        "directors": [
            {
                "name": item,
                "id_number": "",
                "country": "South Africa",
                "is_active": True,
                "is_owner": True,
                "ownership_percentage": None,
                "is_youth": False,
                "is_disabled": False,
                "is_military_veteran": False,
                "is_government_employee": False,
                "verification_status": "Unknown"
            }
            for item in extract_list_by_keywords(text, ["director", "member", "owner"], 10)
        ],
        "ownership_summary": {
            "total_ownership_percentage": None,
            "black_owned": "100" in " ".join(bbbee["details"]),
            "youth_owned": "youth" in text.lower(),
            "township_based": "township" in text.lower(),
            "rural_based": "rural" in text.lower()
        },
        "ai_enrichment": {
            "risk_flags": [],
            "compliance_score": 0,
            "bbbbee_strength_score": 0,
            "operational_readiness_score": 0,
            "tender_competitiveness_score": 0,
            "recommended_actions": []
        },
        "keywords": tokenize(text)[:30]
    }


def pick_best_tender_document(documents):
    if not documents:
        return None

    preferred_types = [
        "tenderNotice",
        "biddingDocuments",
        "technicalSpecifications",
        "evaluationCriteria",
        "billOfQuantity"
    ]

    scored = []
    for doc in documents:
        score = 0
        doc_type = (doc.get("documentType") or "").lower()
        title = (doc.get("title") or "").lower()
        url = (doc.get("url") or "").lower()
        fmt = (doc.get("format") or "").lower()

        if "pdf" in fmt or url.endswith(".pdf"):
            score += 10

        for idx, value in enumerate(preferred_types[::-1]):
            if value.lower() in doc_type:
                score += (idx + 1) * 10

        if any(k in title for k in [
            "bid document", "tender document", "specification",
            "terms of reference", "scope of work", "rfq", "rfp"
        ]):
            score += 20

        scored.append((score, doc))

    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1]


def download_pdf_text_from_url(url):
    if not url:
        return ""

    response = requests.get(url, timeout=40)
    response.raise_for_status()

    content_type = response.headers.get("Content-Type", "").lower()
    if "pdf" not in content_type and not url.lower().endswith(".pdf"):
        return ""

    reader = PdfReader(io.BytesIO(response.content))
    pages = []
    for page in reader.pages:
        pages.append(page.extract_text() or "")

    return "\n".join(pages)


def parse_tender_document_text(text):
    scoring_criteria = []
    specifications = []
    requirements = []
    special_conditions = []
    briefing = {
        "compulsory": False,
        "date": "",
        "venue": ""
    }

    for line in [x.strip() for x in text.splitlines() if x.strip()]:
        ll = line.lower()

        if any(k in ll for k in [
            "80/20", "90/10", "preference point", "specific goals",
            "evaluation criteria", "functionality", "price and specific goals"
        ]):
            scoring_criteria.append(line[:240])

        if any(k in ll for k in [
            "scope of work", "specification", "technical requirement",
            "minimum requirement", "deliverables", "works required"
        ]):
            specifications.append(line[:240])

        if any(k in ll for k in [
            "csd", "tax", "bbbee", "cidb", "ecsa", "sacpcmp",
            "local content", "compulsory briefing", "mandatory"
        ]):
            requirements.append(line[:240])

        if any(k in ll for k in [
            "special condition", "special conditions"
        ]):
            special_conditions.append(line[:240])

        if "briefing" in ll or "site meeting" in ll:
            if "compulsory" in ll:
                briefing["compulsory"] = True
            if not briefing["date"]:
                date_match = re.search(
                    r"((?:\d{4}[-/]\d{2}[-/]\d{2})|(?:\d{2}[-/]\d{2}[-/]\d{4}))",
                    line
                )
                if date_match:
                    briefing["date"] = date_match.group(1)
            if not briefing["venue"] and ":" in line:
                briefing["venue"] = line.split(":", 1)[1].strip()[:180]

    return {
        "scoring_criteria": dedupe_keep_order(scoring_criteria)[:20],
        "specifications": dedupe_keep_order(specifications)[:20],
        "requirements": dedupe_keep_order(requirements)[:25],
        "special_conditions_parsed": dedupe_keep_order(special_conditions)[:12],
        "briefing_parsed": briefing
    }


def estimate_tender_value(title, description, category):
    text = f"{title} {description}".lower()

    low = 50000
    high = 300000
    confidence = "Low"
    reason = "Generic service estimate based on tender wording."

    if "generator" in text:
        low, high = 800000, 3000000
        confidence = "Medium"
        reason = "Generator installations typically fall within this range."
    elif any(k in text for k in ["construction", "building", "infrastructure"]):
        low, high = 500000, 5000000
        confidence = "Medium"
        reason = "Construction and infrastructure tenders are usually medium to high value."
    elif any(k in text for k in ["maintenance", "repair", "servicing"]):
        low, high = 100000, 1000000
        confidence = "Medium"
        reason = "Maintenance and repair contracts vary with scope and contract term."
    elif any(k in text for k in ["truck", "vehicle", "fire truck"]):
        low, high = 1000000, 8000000
        confidence = "High"
        reason = "Specialized vehicles are typically high-value procurements."
    elif any(k in text for k in ["server", "hardware", "storage", "backup appliance"]):
        low, high = 200000, 2000000
        confidence = "Medium"
        reason = "IT infrastructure procurement depends on scale and specification."
    elif category and category.lower() == "goods":
        low, high = 50000, 1000000
        confidence = "Low"
        reason = "General goods procurement estimate."

    return {
        "value_display": f"R{low:,.0f} - R{high:,.0f}",
        "value_source": "estimated",
        "estimation_confidence": confidence,
        "estimation_reason": reason,
        "estimated_value_low": low,
        "estimated_value_high": high,
        "estimated_value_mid": round((low + high) / 2, 0)
    }


def estimate_execution_investment(title, description, category, estimated_low, estimated_high):
    text = f"{title} {description}".lower()

    ratio_low = 0.35
    ratio_high = 0.70
    reason = "Typical execution readiness, procurement, mobilisation, and delivery costs were applied."

    if "generator" in text:
        ratio_low, ratio_high = 0.55, 0.82
        reason = "Generator supply and installation usually require significant equipment, transport, and technical delivery spend."
    elif any(k in text for k in ["construction", "building", "infrastructure"]):
        ratio_low, ratio_high = 0.60, 0.85
        reason = "Construction and infrastructure work generally requires substantial materials, labour, and site mobilisation."
    elif any(k in text for k in ["maintenance", "repair", "servicing"]):
        ratio_low, ratio_high = 0.40, 0.70
        reason = "Maintenance and repair contracts usually carry labour, tools, materials, and travel costs."
    elif any(k in text for k in ["truck", "vehicle", "fire truck"]):
        ratio_low, ratio_high = 0.70, 0.92
        reason = "Vehicle and specialized equipment tenders often require high capital outlay before delivery."
    elif any(k in text for k in ["server", "hardware", "storage", "backup appliance"]):
        ratio_low, ratio_high = 0.65, 0.88
        reason = "Hardware and IT supply contracts typically need significant procurement capital and logistics."
    elif category and category.lower() == "services":
        ratio_low, ratio_high = 0.30, 0.60
        reason = "Service tenders usually need less equipment spend, but still require staffing, compliance, and delivery overhead."

    low = round(estimated_low * ratio_low, 0)
    high = round(estimated_high * ratio_high, 0)
    mid = round((low + high) / 2, 0)

    return {
        "execution_investment_low": low,
        "execution_investment_high": high,
        "execution_investment_mid": mid,
        "execution_investment_display": f"R{low:,.0f} - R{high:,.0f}",
        "execution_investment_reason": reason
    }


def infer_requirements(tender_text, profile_text):
    checks = []
    text = tender_text.lower()
    profile = profile_text.lower()

    rules = [
        ("CSD registration", ["csd"]),
        ("Tax compliance", ["tax"]),
        ("B-BBEE evidence", ["b-bbee", "bbbee"]),
        ("CIDB", ["cidb"]),
        ("Compulsory briefing", ["briefing", "site meeting"]),
        ("Local content forms", ["local content", "sbd 6.2"]),
        ("Professional registration", ["professional registration", "sacpcmp", "ecsa", "preng"]),
        ("Health and safety", ["safety", "ohs", "health and safety"])
    ]

    for name, keys in rules:
        if any(k in text for k in keys):
            if any(k in profile for k in keys if k not in ["briefing", "site meeting", "local content", "sbd 6.2"]):
                status = "Likely met"
                comment = "Related evidence appears in the profile."
            elif name in ["Compulsory briefing", "Local content forms"]:
                status = "Action required"
                comment = "TenderAI detected this in the tender. Confirm attendance/forms during bid preparation."
            else:
                status = "Check"
                comment = "TenderAI detected the requirement but could not confirm evidence from the profile."

            checks.append({
                "name": name,
                "status": status,
                "comment": comment
            })

    return checks[:8]


def infer_preference_model(value_mid, profile_text):
    model = "Estimated 80/20" if value_mid <= 50000000 else "Estimated 90/10"

    if "bbbee" in profile_text.lower() or "b-bbee" in profile_text.lower():
        comment = "Profile appears to include B-BBEE-related evidence, which may support specific-goal scoring if the tender documents allow it."
    else:
        comment = "TenderAI could not confirm B-BBEE-specific evidence from the profile. Confirm the tender's specific goals and proof rules."

    return model, comment


def calculate_fit(profile_keywords, prompt_keywords, tender_text, category, requirement_checks, document_parse=None):
    tokens = set(tokenize(tender_text))
    combined = list(dict.fromkeys((profile_keywords or []) + (prompt_keywords or [])))
    matched = sorted(set(combined).intersection(tokens))

    base_score = (len(matched) / max(len(set(combined)), 1)) * 100
    bonus = 0

    if category and category.lower() in ["works", "services"]:
        bonus += 10

    intent_keywords = [
        "installation", "maintenance", "repair", "construction",
        "electrical", "generator", "supply"
    ]
    bonus += sum(1 for k in intent_keywords if k in tokens) * 4

    if len(requirement_checks) >= 3:
        bonus += 4

    document_parse = document_parse or {}
    if document_parse.get("scoring_criteria"):
        bonus += 6
    if document_parse.get("specifications"):
        bonus += 6
    if document_parse.get("requirements"):
        bonus += 6

    score = round(min(base_score + bonus, 100), 1)

    if score >= 70:
        band = "High fit"
    elif score >= 40:
        band = "Medium fit"
    else:
        band = "Low fit"

    return score, band, matched


def compute_bid_readiness(requirement_checks):
    if not requirement_checks:
        return (
            "Early-stage",
            "Limited tender-document requirements were detected from the available notice text."
        )

    action_required = sum(1 for r in requirement_checks if r["status"] == "Action required")
    checks = sum(1 for r in requirement_checks if r["status"] == "Check")

    if action_required == 0 and checks <= 1:
        return "Strong", "The profile appears broadly aligned with the detected requirement set."
    if action_required <= 1 and checks <= 3:
        return "Moderate", "Some requirements need confirmation or bid preparation work."
    return "Needs work", "Several requirements or actions need attention before submission."


def infer_risk_and_difficulty(description, requirement_checks):
    text = (description or "").lower()
    risk_score = 0
    diff_score = 0

    if any(k in text for k in ["compulsory briefing", "site meeting", "mandatory", "compulsory"]):
        risk_score += 2
        diff_score += 1
    if any(k in text for k in ["cidb", "local content", "electrical", "generator", "specialized", "specialised"]):
        risk_score += 2
        diff_score += 2
    if any(k in text for k in ["construction", "infrastructure", "server", "hardware", "truck"]):
        diff_score += 2
    if len(requirement_checks) >= 4:
        risk_score += 1
        diff_score += 1

    if risk_score >= 4:
        risk_level = "High"
        risk_reason = "The tender appears to include multiple conditions, specialized requirements, or mandatory bid risks."
    elif risk_score >= 2:
        risk_level = "Medium"
        risk_reason = "The tender has some conditions that may increase compliance or delivery risk."
    else:
        risk_level = "Low"
        risk_reason = "The tender appears relatively straightforward based on the available notice content."

    if diff_score >= 4:
        difficulty_level = "High"
        difficulty_reason = "The tender likely requires stronger capability proof and tighter delivery planning."
    elif diff_score >= 2:
        difficulty_level = "Medium"
        difficulty_reason = "The tender seems achievable but may require stronger documentation and positioning."
    else:
        difficulty_level = "Low"
        difficulty_reason = "The tender appears comparatively accessible based on the available text."

    return risk_level, risk_reason, difficulty_level, difficulty_reason


def build_ai_summary(title, description, buyer, category):
    desc = (description or "").strip()
    if desc:
        short_desc = desc[:240] + ("..." if len(desc) > 240 else "")
        return f"{title} issued by {buyer} appears to be a {category.lower() if category else 'procurement'} opportunity focused on: {short_desc}"
    return f"{title} issued by {buyer} appears to be a {category.lower() if category else 'procurement'} opportunity with limited public description."


def enrich_tender(item, profile=None, prompt=""):
    tender = item.get("tender", {}) if isinstance(item, dict) else {}
    buyer = item.get("buyer", {}) if isinstance(item, dict) else {}
    tender_period = tender.get("tenderPeriod", {}) if isinstance(tender, dict) else {}
    value = tender.get("value", {}) if isinstance(tender, dict) else {}

    description = tender.get("description", "") or ""
    title = tender.get("title", "") or ""
    buyer_name = buyer.get("name", "") or ""
    category = tender.get("mainProcurementCategory", "") or ""
    province = infer_province(f"{buyer_name} {description}")
    combined_text = f"{title} {description} {buyer_name} {category}"

    documents = tender.get("documents", []) or []
    best_document = pick_best_tender_document(documents)
    best_document_url = best_document.get("url", "") if best_document else ""
    best_document_title = best_document.get("title", "") if best_document else ""

    document_text = ""
    document_parse = {
        "scoring_criteria": [],
        "specifications": [],
        "requirements": [],
        "special_conditions_parsed": [],
        "briefing_parsed": {"compulsory": False, "date": "", "venue": ""}
    }

    try:
        if best_document_url:
            document_text = download_pdf_text_from_url(best_document_url)
            if document_text:
                document_parse = parse_tender_document_text(document_text)
    except Exception:
        pass

    profile_text = profile["text"] if profile else ""
    profile_keywords = profile["meta"]["keywords"] if profile else []
    prompt_keywords = tokenize(prompt)[:10]
    requirement_checks = infer_requirements(combined_text + " " + document_text, profile_text)

    fit_score, fit_band, matched_keywords = calculate_fit(
        profile_keywords=profile_keywords,
        prompt_keywords=prompt_keywords,
        tender_text=combined_text + " " + document_text,
        category=category,
        requirement_checks=requirement_checks,
        document_parse=document_parse
    )

    published_value = value.get("amount")
    published_currency = value.get("currency")
    estimation = estimate_tender_value(title, description + " " + document_text[:1500], category)

    if published_value and published_value > 0:
        value_display = f"R{published_value:,.0f}"
        value_source = "published"
        estimation_confidence = "High"
        estimation_reason = "Published by tender source."
        estimated_value_low = published_value
        estimated_value_high = published_value
        estimated_value_mid = published_value
    else:
        value_display = estimation["value_display"]
        value_source = estimation["value_source"]
        estimation_confidence = estimation["estimation_confidence"]
        estimation_reason = estimation["estimation_reason"]
        estimated_value_low = estimation["estimated_value_low"]
        estimated_value_high = estimation["estimated_value_high"]
        estimated_value_mid = estimation["estimated_value_mid"]

    execution = estimate_execution_investment(
        title=title,
        description=description + " " + document_text[:1500],
        category=category,
        estimated_low=estimated_value_low,
        estimated_high=estimated_value_high
    )

    ai_summary = build_ai_summary(title, description or document_text[:350], buyer_name, category)
    risk_level, risk_reason, difficulty_level, difficulty_reason = infer_risk_and_difficulty(
        description + " " + document_text[:1500],
        requirement_checks
    )
    preferential_model, preference_comment = infer_preference_model(
        estimated_value_mid,
        profile_text
    )
    bid_readiness, bid_readiness_comment = compute_bid_readiness(requirement_checks)

    win_probability = max(
        10,
        min(
            92,
            round(
                fit_score
                - (5 if risk_level == "High" else 0)
                + (4 if bid_readiness == "Strong" else 0),
                0
            )
        )
    )

    tender_id = item.get("ocid") or str(uuid.uuid4())

    enriched = {
        "id": tender_id,
        "ocid": item.get("ocid") if isinstance(item, dict) else None,
        "title": title,
        "buyer": buyer_name,
        "description": description,
        "status": tender.get("status"),
        "category": category,
        "province": province,
        "close_date": tender_period.get("endDate"),
        "date_published": item.get("date"),
        "value_amount": published_value,
        "value_currency": published_currency,
        "value_display": value_display,
        "value_source": value_source,
        "estimation_confidence": estimation_confidence,
        "estimation_reason": estimation_reason,
        "estimated_value_low": estimated_value_low,
        "estimated_value_high": estimated_value_high,
        "estimated_value_mid": estimated_value_mid,
        "fit_score": fit_score,
        "fit_band": fit_band,
        "win_probability": win_probability,
        "matched_keywords": matched_keywords,
        "execution_investment_low": execution["execution_investment_low"],
        "execution_investment_high": execution["execution_investment_high"],
        "execution_investment_mid": execution["execution_investment_mid"],
        "execution_investment_display": execution["execution_investment_display"],
        "execution_investment_reason": execution["execution_investment_reason"],
        "ai_summary": ai_summary,
        "key_requirements": [r["name"] for r in requirement_checks],
        "requirement_checks": requirement_checks,
        "risk_level": risk_level,
        "risk_reason": risk_reason,
        "difficulty_level": difficulty_level,
        "difficulty_reason": difficulty_reason,
        "preferential_model": preferential_model,
        "preference_comment": preference_comment,
        "bid_readiness": bid_readiness,
        "bid_readiness_comment": bid_readiness_comment,
        "tender_number": tender.get("id") or title,
        "organ_of_state": buyer_name,
        "tender_type": category or "Unspecified",
        "location_of_service_delivery": tender.get("deliveryLocation") or province,
        "special_conditions": " | ".join(document_parse["special_conditions_parsed"][:4]) or "Review full tender documents for mandatory conditions and returnable schedules.",
        "contact_person": (tender.get("contactPerson") or {}).get("name", "Not provided in current API notice"),
        "contact_email": (tender.get("contactPerson") or {}).get("email", "Not provided in current API notice"),
        "contact_phone": (tender.get("contactPerson") or {}).get("telephoneNumber", "Not provided in current API notice"),
        "briefing_session_details": (
            f'Compulsory: {"Yes" if document_parse["briefing_parsed"]["compulsory"] else "Unknown"}; '
            f'Date: {document_parse["briefing_parsed"]["date"] or "Not detected"}; '
            f'Venue: {document_parse["briefing_parsed"]["venue"] or "Not detected"}'
        ),
        "tender_document_url": best_document_url,
        "documents": documents,
        "best_document_url": best_document_url,
        "best_document_title": best_document_title,
        "parsed_scoring_criteria": document_parse["scoring_criteria"],
        "parsed_specifications": document_parse["specifications"],
        "parsed_requirements": document_parse["requirements"],
        "parsed_special_conditions": document_parse["special_conditions_parsed"],
        "parsed_briefing": document_parse["briefing_parsed"],
    }

    TENDER_CACHE[tender_id] = enriched
    return enriched


def build_analytics(tenders):
    sector_counter = Counter([t.get("category") or "Unspecified" for t in tenders])
    province_counter = Counter([t.get("province") or "Unspecified" for t in tenders])

    by_sector = [{"label": k, "value": v} for k, v in sector_counter.most_common(6)]
    by_province = [{"label": k, "value": v} for k, v in province_counter.most_common(6)]

    avg_value = round(
        sum(t.get("estimated_value_mid", 0) for t in tenders) / max(len(tenders), 1),
        0
    )

    top_category = by_sector[0]["label"] if by_sector else "No dominant sector"
    top_category_share = round((by_sector[0]["value"] / max(len(tenders), 1)) * 100, 0) if by_sector else 0

    top_province = by_province[0]["label"] if by_province else "No dominant province"
    top_province_share = round((by_province[0]["value"] / max(len(tenders), 1)) * 100, 0) if by_province else 0

    return {
        "by_sector": by_sector,
        "by_province": by_province,
        "trend_insights": {
            "top_category_insight": f"{top_category} accounts for roughly {top_category_share}% of the current opportunity set.",
            "top_province_insight": f"{top_province} contributes roughly {top_province_share}% of the observed opportunities.",
            "value_insight": f"The average estimated contract value in the current scan is about R{avg_value:,.0f}."
        }
    }


def count_closing_soon(tenders):
    count = 0
    now = datetime.now(timezone.utc)

    for t in tenders:
        close_date = t.get("close_date")
        if not close_date:
            continue
        try:
            dt = datetime.fromisoformat(close_date.replace("Z", "+00:00"))
            delta_days = (dt - now).days
            if 0 <= delta_days <= 7:
                count += 1
        except Exception:
            pass

    return count


@app.get("/")
def home():
    return render_template("home.html")


@app.get("/profiles")
def profiles_page():
    return render_template("profiles.html")


@app.get("/tenders")
def tenders_page():
    return render_template("feed.html")


@app.get("/tender/<tender_id>")
def tender_detail_page(tender_id):
    tender = TENDER_CACHE.get(tender_id)
    if not tender:
        return redirect(url_for("tenders_page"))
    return render_template("tender_detail.html", tender=tender)


@app.get("/api/profiles")
def api_profiles():
    profiles = list(PROFILE_STORE.values())

    def get_main_groups(meta):
        return [
            x.get("main_group", "")
            for x in meta.get("industry_classification", [])
            if x.get("main_group")
        ]

    def get_divisions(meta):
        return [
            x.get("division", "")
            for x in meta.get("industry_classification", [])
            if x.get("division")
        ]

    return jsonify({
        "status": "ok",
        "profiles": [
            {
                "id": p["id"],
                "name": p["name"],
                "company_name": p["meta"]["supplier_identification"].get("legal_name", "Unknown"),
                "supplier_active_status": "Yes" if p["meta"]["supplier_identification"].get("is_active") else "No",
                "supplier_sub_type": p["meta"]["supplier_identification"].get("supplier_sub_type", "Unknown"),
                "country_of_origin": p["meta"]["supplier_identification"].get("country_of_origin", "Unknown"),
                "government_employee": "Yes" if p["meta"]["supplier_identification"].get("government_employee") else "No",
                "overall_tax_status": p["meta"]["tax_information"].get("tax_compliance_status", "Unknown"),
                "sars_registration_status": "Yes" if p["meta"]["tax_information"].get("is_registered_with_sars") else "No",
                "industry_classification": ", ".join(get_main_groups(p["meta"])[:3]) or "Unknown",
                "industry_main_groups": get_main_groups(p["meta"]),
                "industry_divisions": get_divisions(p["meta"]),
                "address_information": (
                    p["meta"]["address_information"][0].get("address_line_1", "Unknown")
                    if p["meta"].get("address_information") else "Unknown"
                ),
                "bbbee_information": (
                    p["meta"]["bbbbee_information"].get("verification_status")
                    or p["meta"]["bbbbee_information"].get("certificate_number")
                    or "Unknown"
                ),
                "bbbee_details": [
                    f'Certificate: {p["meta"]["bbbbee_information"].get("certificate_number", "Unknown")}',
                    f'Issue: {p["meta"]["bbbbee_information"].get("issue_date", "Unknown")}',
                    f'Expiry: {p["meta"]["bbbbee_information"].get("expiry_date", "Unknown")}'
                ],
                "ownership_information": str(p["meta"].get("ownership_summary", {})),
                "directors_members_owners": [x.get("name", "") for x in p["meta"].get("directors", []) if x.get("name")],
                "accreditations": [
                    {
                        "status": x.get("status", "Unknown"),
                        "description": x.get("description", "Unknown"),
                        "expiration_date": x.get("expiry_date", "Unknown")
                    }
                    for x in p["meta"].get("accreditations", [])
                ],
                "associations": [],
                "commodities": p["meta"].get("keywords", [])[:8],
                "provinces": [
                    x.get("province", "")
                    for x in p["meta"].get("address_information", [])
                    if x.get("province")
                ],
                "keywords": p["meta"].get("keywords", [])[:12],
                "uploaded_at": p["uploaded_at"]
            }
            for p in profiles
        ]
    })


@app.post("/api/profiles")
def api_upload_profile():
    if "profile_pdf" not in request.files:
        return jsonify({"status": "error", "error": "No PDF uploaded"}), 400

    file = request.files["profile_pdf"]
    if not file.filename.lower().endswith(".pdf"):
        return jsonify({"status": "error", "error": "Upload a PDF file"}), 400

    text = extract_pdf_text(file)
    meta = parse_profile_metadata(text)

    profile_id = str(uuid.uuid4())
    PROFILE_STORE[profile_id] = {
        "id": profile_id,
        "name": file.filename,
        "text": text,
        "meta": meta,
        "uploaded_at": datetime.now(timezone.utc).isoformat()
    }

    return jsonify({"status": "ok", "profile_id": profile_id})


@app.delete("/api/profiles/<profile_id>")
def api_delete_profile(profile_id):
    PROFILE_STORE.pop(profile_id, None)
    return jsonify({"status": "ok"})


@app.post("/api/score")
def api_score():
    body = request.get_json(silent=True) or {}
    profile_id = body.get("profile_id")
    prompt = body.get("prompt", "")
    date_from = body.get("date_from", "2026-01-01")
    date_to = body.get("date_to", "2026-03-17")
    page_number = int(body.get("page_number", 1))
    page_size = int(body.get("page_size", 10))

    profile = PROFILE_STORE.get(profile_id)
    if not profile:
        return jsonify({"status": "error", "error": "Profile not found"}), 404

    try:
        releases, _ = fetch_tenders(date_from, date_to, page_number, page_size)
        tenders = [enrich_tender(item, profile=profile, prompt=prompt) for item in releases]
        tenders = sorted(
            tenders,
            key=lambda x: (x["fit_score"], x["win_probability"]),
            reverse=True
        )

        analytics = build_analytics(tenders)

        return jsonify({
            "status": "ok",
            "profile_name": profile["name"],
            "prompt": prompt,
            "summary": {
                "returned_tenders": len(tenders),
                "high_fit": sum(1 for t in tenders if t["fit_band"] == "High fit"),
                "medium_fit": sum(1 for t in tenders if t["fit_band"] == "Medium fit"),
                "low_fit": sum(1 for t in tenders if t["fit_band"] == "Low fit"),
                "closing_soon": count_closing_soon(tenders),
                "average_estimated_value_mid": round(
                    sum(t["estimated_value_mid"] for t in tenders) / max(len(tenders), 1),
                    0
                )
            },
            "analytics": analytics,
            "tenders": tenders
        })
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.get("/api/tenders")
def api_tenders():
    date_from = request.args.get("date_from", "2026-01-01")
    date_to = request.args.get("date_to", "2026-03-17")
    page_number = int(request.args.get("page_number", 1))
    page_size = int(request.args.get("page_size", 20))
    province_filter = request.args.get("province", "").strip().lower()
    industry_filter = request.args.get("industry", "").strip().lower()

    try:
        releases, _ = fetch_tenders(date_from, date_to, page_number, page_size)
        tenders = [enrich_tender(item, profile=None, prompt="") for item in releases]

        if province_filter:
            tenders = [t for t in tenders if province_filter in t.get("province", "").lower()]

        if industry_filter:
            tenders = [
                t for t in tenders
                if industry_filter in t.get("category", "").lower()
                or industry_filter in t.get("description", "").lower()
            ]

        return jsonify({
            "status": "ok",
            "tenders": tenders
        })
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.get("/api/tender/<tender_id>")
def api_tender_detail(tender_id):
    tender = TENDER_CACHE.get(tender_id)
    if not tender:
        return jsonify({"status": "error", "error": "Tender not found"}), 404
    return jsonify({"status": "ok", "tender": tender})


@app.post("/api/advise")
def api_advise():
    body = request.get_json(silent=True) or {}
    tender = body.get("tender", {}) or {}
    profile_id = body.get("profile_id")
    profile = PROFILE_STORE.get(profile_id)

    profile_text = profile["text"] if profile else ""
    title = str(tender.get("title", ""))
    description = str(tender.get("description", ""))
    category = str(tender.get("category", ""))
    matched_keywords = tender.get("matched_keywords", []) or []

    advice = []
    required_capabilities = tender.get("key_requirements", []) or []

    if not matched_keywords:
        advice.append("Sharpen your capability statement so it mirrors the exact tender language more directly.")
    else:
        advice.append("Reflect the strongest matched keywords in your executive summary, methodology, and pricing narrative.")

    if "generator" in f"{title} {description}".lower():
        advice.append("Include generator-specific references, electrical compliance evidence, and technical delivery capability.")
        required_capabilities.extend([
            "Electrical compliance certificate",
            "Generator installation references",
            "Technical methodology"
        ])

    if category.lower() == "works":
        advice.append("Show site methodology, supervision structure, safety planning, and mobilisation readiness.")
        required_capabilities.extend([
            "Health and safety file",
            "Construction methodology",
            "Site mobilisation plan"
        ])

    if category.lower() == "services":
        advice.append("Show turnaround times, staffing depth, response processes, and geographic operating capacity.")
        required_capabilities.extend([
            "Service delivery plan",
            "Team CVs",
            "Operational response plan"
        ])

    if "bbbee" not in profile_text.lower() and "b-bbee" not in profile_text.lower():
        advice.append("Confirm whether you have current B-BBEE evidence available if the tender allocates points to specific goals.")

    if tender.get("bid_readiness") == "Needs work":
        advice.append("Do not treat this as submission-ready yet. Close the missing evidence gaps before committing bid resources.")

    should_apply = (
        "Apply if you can close the highlighted compliance and documentation gaps quickly."
        if tender.get("fit_score", 0) >= 55
        else "Monitor rather than apply immediately unless you have stronger supporting evidence than TenderAI could detect."
    )
    risk_comment = f"Current risk view: {tender.get('risk_level', 'Unknown')} risk. {tender.get('risk_reason', '')}"
    competitor_assumption = "Expect competition from suppliers with stronger reference portfolios, complete compliance packs, and closer scope alignment."

    required_capabilities = dedupe_keep_order(required_capabilities)[:10]

    return jsonify({
        "status": "ok",
        "should_apply": should_apply,
        "risk_comment": risk_comment,
        "competitor_assumption": competitor_assumption,
        "advice": advice,
        "required_capabilities": required_capabilities
    })


@app.post("/api/service-request")
def api_service_request():
    body = request.get_json(silent=True) or {}
    name = body.get("name", "Unknown")
    company = body.get("company", "Unknown")
    tender = body.get("tender", {}) or {}

    reference = f"TAI-{abs(hash((name, company, tender.get('ocid', 'NA')))) % 1000000:06d}"

    return jsonify({
        "status": "ok",
        "reference": reference
    })

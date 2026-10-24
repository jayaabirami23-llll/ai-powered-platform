
from flask import Flask, request, render_template_string
from werkzeug.utils import secure_filename
from pymongo import MongoClient
from pathlib import Path
from PIL import Image
from difflib import SequenceMatcher
from datetime import datetime, timezone
import pymupdf
import pytesseract
import re
import uuid
import os
import csv
import io

app = Flask(__name__)

# ---------------- CONFIGURATION ----------------

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_FOLDER = BASE_DIR / "uploads"

UPLOAD_FOLDER.mkdir(exist_ok=True)

app.config["UPLOAD_FOLDER"] = str(UPLOAD_FOLDER)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024

ALLOWED_EXTENSIONS = {"pdf", "png", "jpg", "jpeg"}

# Tesseract OCR path for Windows
TESSERACT_PATH = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

if os.path.exists(TESSERACT_PATH):
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_PATH


# ---------------- MONGODB CONNECTION ----------------

mongo_client = MongoClient(
    "mongodb://127.0.0.1:27017/",
    serverSelectionTimeoutMS=3000
)

db = mongo_client["AICTE_SmartVerify"]

institutions = db["institutions"]
applications = db["applications"]
verification_checks = db["verification_checks"]
signature_references = db["signature_references"]
validation_reports = db["validation_reports"]



# ---------------- HELPER FUNCTIONS ----------------

def allowed_file(filename):
    return (
        "." in filename
        and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS
    )


def normalize_text(text):
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def similarity_score(first, second):
    first = normalize_text(first)
    second = normalize_text(second)

    if not first or not second:
        return 0

    return round(
        SequenceMatcher(None, first, second).ratio() * 100,
        2
    )


def extract_application_id(text):
    # First try to find an ID next to its label.
    pattern = (
        r"(?:APPLICATION\s*(?:ID|NO|NUMBER)|"
        r"APP(?:LICATION)?[_\s-]*ID)"
        r"\s*[:#-]?\s*"
        r"([A-Z0-9][A-Z0-9/-]*(?:-[A-Z0-9]+)*)"
    )

    match = re.search(pattern, text, re.IGNORECASE)

    if match:
        return match.group(1).strip(" :-")

    # Fallback for common demo ID formats.
    pattern = r"\b(?:APP|AICTE)[A-Z0-9]*(?:-[A-Z0-9]+)*\b"
    match = re.search(pattern, text, re.IGNORECASE)

    return match.group(0) if match else None


def extract_document_text(file_path, extension):
    text = ""

    if extension == "pdf":
        with pymupdf.open(file_path) as pdf:
            for page in pdf:
                page_text = page.get_text().strip()

                if len(page_text) > 20:
                    text += page_text + "\n"
                else:
                    # OCR for scanned PDF pages
                    pix = page.get_pixmap(
                        matrix=pymupdf.Matrix(2, 2),
                        alpha=False
                    )

                    image = Image.frombytes(
                        "RGB",
                        (pix.width, pix.height),
                        pix.samples
                    )

                    text += pytesseract.image_to_string(image) + "\n"

    else:
        with Image.open(file_path) as image:
            text = pytesseract.image_to_string(image)

    return text.strip()


def name_found_in_text(name, text):
    normalized_name = normalize_text(name)
    normalized_text = normalize_text(text)

    if normalized_name and normalized_name in normalized_text:
        return True

    # Handle small OCR spelling differences.
    for line in text.splitlines():
        if similarity_score(name, line) >= 85:
            return True

    return False

from PIL import Image
from pathlib import Path
import os


def calculate_signature_hash(image_path):
    """Create a simple visual fingerprint of a signature image."""
    with Image.open(image_path) as image:
        image = image.convert("L").resize((16, 16))
        pixels = list(image.getdata())

    average = sum(pixels) / len(pixels)

    # 256-bit average-hash fingerprint
    return "".join("1" if pixel >= average else "0" for pixel in pixels)


def compare_signature_hashes(hash1, hash2):
    """Return a visual similarity score between 0 and 100."""
    if not hash1 or not hash2 or len(hash1) != len(hash2):
        return 0.0

    differences = sum(a != b for a, b in zip(hash1, hash2))
    return round((1 - differences / len(hash1)) * 100, 2)



def check_duplicate_signature(signature_image_path):
    """Compare against explicitly trusted signature references only."""

    trusted_references = list(
        signature_references.find(
            {"record_source": "Trusted signature reference"},
            {"_id": 0}
        )
    )

    # No trusted reference: do not calculate or report a match.
    if not trusted_references:
        return {
            "possible_duplicate": False,
            "similarity_score": None,
            "matched_reference_id": None,
            "status": "reference_unavailable",
            "message": (
                "Duplicate check not performed: "
                "no trusted signature reference is available."
            )
        }

    current_hash = calculate_signature_hash(signature_image_path)
    best_match = None
    highest_score = 0.0

    for record in trusted_references:
        saved_hash = record.get("signature_hash", "")
        score = compare_signature_hashes(current_hash, saved_hash)

        if score > highest_score:
            highest_score = score
            best_match = record

    possible_duplicate = highest_score >= 90

    return {
        "possible_duplicate": possible_duplicate,
        "similarity_score": highest_score,
        "matched_reference_id": (
            best_match.get("signature_id") if best_match else None
        ),
        "status": (
            "review_required" if possible_duplicate
            else "no_high_match"
        ),
        "message": (
            "High visual similarity found; manual review required."
            if possible_duplicate
            else "No high-similarity trusted reference found."
        )
    }



def save_signature_reference(signature_id, signature_image_path):
    """Save a trusted signature reference in MongoDB."""
    signature_hash = calculate_signature_hash(signature_image_path)

    signature_references.update_one(
        {"signature_id": signature_id},
        {
            "$set": {
                "signature_id": signature_id,
                "signature_hash": signature_hash,
                "record_source": "Trusted signature reference"
            }
        },
        upsert=True
    )

    return True

def extract_signature_crop(file_path, output_path, page_number=0):
    """Crop the approximate handwritten-signature area from page 1."""
    from pathlib import Path
    from PIL import Image

    with pymupdf.open(file_path) as pdf:
        if page_number < 0 or page_number >= len(pdf):
            raise ValueError("Invalid PDF page number.")

        page = pdf[page_number]
        pix = page.get_pixmap(
            matrix=pymupdf.Matrix(2, 2),
            alpha=False
        )

        image = Image.frombytes(
            "RGB",
            (pix.width, pix.height),
            pix.samples
        )

    # Approximate crop coordinates based on the supplied page image.
    width, height = image.size
    left = int(width * 0.48)
    top = int(height * 0.82)
    right = int(width * 0.80)
    bottom = int(height * 0.95)

    signature_crop = image.crop((left, top, right, bottom))

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    signature_crop.save(output)

    return str(output)




# ---------------- DOCUMENT VERIFICATION ----------------

def verify_document(document_text):
    extracted_id = extract_application_id(document_text)

    application = None
    institution = None
    institution_match = False
    institution_score = 0

    # Look up the application in MongoDB.
    if extracted_id:
        application = applications.find_one(
            {"application_id": extracted_id},
            {"_id": 0}
        )

        if not application:
            application = applications.find_one(
                {
                    "application_id": {
                        "$regex": "^" + re.escape(extracted_id) + "$",
                        "$options": "i"
                    }
                },
                {"_id": 0}
            )

    # Find the institution linked to that application.
    if application:
        institution = institutions.find_one(
            {"institution_id": application.get("institution_id")},
            {"_id": 0}
        )

    # Compare the institution name with the uploaded document.
    if institution:
        expected_name = institution.get("institution_name", "")

        institution_match = name_found_in_text(
            expected_name,
            document_text
        )

        institution_score = (
            100 if institution_match
            else max(
                [
                    similarity_score(expected_name, line)
                    for line in document_text.splitlines()
                ],
                default=0
            )
        )

    # Compare important fields with the database reference.
    def normalize_value(value):
        return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())

    normalized_document = normalize_value(document_text)
    reference_mismatches = []

    if application:
        fields_to_check = [
            ("academic_year", "Academic Year"),
            ("course", "Course"),
            ("program", "Program"),
            ("approved_intake_2026_27", "Approved Intake 2026-27")
        ]

        for field, label in fields_to_check:
            expected_value = application.get(field)

            if expected_value is not None and str(expected_value).strip():
                normalized_expected = normalize_value(expected_value)

                if normalized_expected not in normalized_document:
                    reference_mismatches.append(label)

    # Decide the verification result.
    if not extracted_id:
        status = "Manual Review"
        reason = "Application ID could not be extracted from the document."

    elif not application:
        status = "Manual Review"
        reason = "Application ID was not found in the reference database."

    elif not institution:
        status = "Manual Review"
        reason = "The linked institution record was not found."

    elif not institution_match:
        status = "Mismatch"
        reason = "Institution name does not match the reference record."
    
    elif reference_mismatches:
        status = "Mismatch"
        reason = (
            "Reference details were not found in the uploaded document: "
            + ", ".join(reference_mismatches)
        )

    else:
        status = "Match"
        reason = "Application ID and institution name match the reference records."

    return {
        "status": status,
        "reason": reason,
        "application_id": extracted_id or "Not detected",
        "application_found": bool(application),
        "institution_name": (
            institution.get("institution_name", "Not available")
            if institution
            else "Not found in reference records"
        ),
        "institution_match": institution_match,
        "institution_score": institution_score,
        "application_type": (
            application.get("application_type", "Not available")
            if application
            else "Not available"
        ),
        "academic_year": (
            application.get("academic_year", "Not available")
            if application
            else "Not available"
        ),
        "extracted_text": document_text
    }


# ---------------- HTML PAGE ----------------

HTML_PAGE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AICTE SmartVerify</title>

    <style>
        * { box-sizing: border-box; }

        body {
            margin: 0;
            font-family: Arial, sans-serif;
            background: #f2f5fa;
            color: #18243a;
        }

        header {
            background: #12264a;
            color: white;
            text-align: center;
            padding: 24px;
        }

        main {
            max-width: 850px;
            margin: 30px auto;
            padding: 0 16px;
        }

        .card {
            background: white;
            padding: 24px;
            margin-bottom: 20px;
            border-radius: 12px;
            box-shadow: 0 3px 12px #0000000d;
        }

        input[type=file] {
            display: block;
            margin: 18px 0;
            max-width: 100%;
        }

        button {
            background: #1768d2;
            color: white;
            padding: 12px 20px;
            border: none;
            border-radius: 7px;
            cursor: pointer;
        }

        .status {
            padding: 12px;
            border-radius: 8px;
            font-weight: bold;
        }

        .Match {
            background: #d9f5e3;
            color: #145c32;
        }

        .Mismatch {
            background: #ffe1e1;
            color: #8a2020;
        }

        .Manual-Review {
            background: #fff0c9;
            color: #765100;
        }

        .error {
            color: #a32020;
        }

        .field {
            margin: 12px 0;
        }

        pre {
            white-space: pre-wrap;
            overflow-wrap: anywhere;
            background: #f4f6fa;
            padding: 14px;
            border-radius: 8px;
            max-height: 300px;
            overflow-y: auto;
        }

        .note {
            color: #58657a;
            font-size: 13px;
            line-height: 1.5;
        }
    </style>
</head>

<body>
    <header>
        <h1>AICTE SmartVerify</h1>
        <p>AI-powered document validation prototype</p>
    </header>

    <main>
        <section class="card">
            <h2>Upload Application Document</h2>

            <p>Upload a PDF or image for document verification.</p>

            <form action="/verify" method="POST"
                  enctype="multipart/form-data">

                <input type="file" name="document"
                       accept=".pdf,.png,.jpg,.jpeg" required>

                <button type="submit">Upload and Verify</button>
            </form>
            <div style="margin-top: 20px;">
            <a href="/dashboard"
            style="display: inline-block;
              background: #198754;
              color: white;
              padding: 12px 20px;
              text-decoration: none;
              border-radius: 6px;">
        View Dashboard
    </a>
</div>

            <p class="note">
                Accepted formats: PDF, PNG, JPG, JPEG.
                Maximum size: 10 MB.
            </p>
        </section>

        {% if error %}
        <section class="card">
            <p class="error">{{ error }}</p>
        </section>
        {% endif %}

        {% if result %}
        <section class="card">
            <h2>Verification Result</h2>

            <div class="status {{ result.status|replace(' ', '-') }}">
                Status: {{ result.status }}
            </div>

            <p>{{ result.reason }}</p>

            <div class="field"
                <strong>Application ID:</strong>
                {{ result.application_id }}
            </div>

            <div class="field">
                <strong>Application found:</strong>
                {{ "Yes" if result.application_found else "No" }}
            </div>

            <div class="field">
                <strong>Institution:</strong>
                {{ result.institution_name }}
            </div>

            <div class="field">
                <strong>Institution match:</strong>
                {{ "Yes" if result.institution_match else "Not confirmed" }}
            </div>

            <div class="field">
                <strong>Application type:</strong>
                {{ result.application_type }}
            </div>

            <div class="field">
                <strong>Academic year:</strong>
                {{ result.academic_year }}
            </div>

            <h3>OCR Extracted Text</h3>
            <pre>{{ result.extracted_text }}</pre>

            <p class="note">
                Prototype result only. A mismatch or missing record
                is not proof of fraud. Human review is required before
                an official decision.
            </p>
            
            <h3>Signature Duplicate Detection Report</h3>

            {% if result.signature_report %}
            
            <div class="field">
                <strong>Possible Duplicate:</strong>
                {% if result.signature_report.status == "reference_unavailable" %}
                    Not evaluated — trusted reference required
                {% elif result.signature_report.possible_duplicate %}
                    Yes — manual review required
                {% else %}
                    No high-similarity match found
                {% endif %}
            </div>
 
            <div class="field">
                <strong>Similarity Score:</strong>
                {% if result.signature_report.similarity_score is not none %}
                {{ result.signature_report.similarity_score }}%
                {% else %}
                    Not available
                {% endif %}
            </div>


            <div class="field">
                <strong>Matched Reference ID:</strong>
                {{ result.signature_report.matched_reference_id or "No matching reference" }}
            </div>

            <div class="field">
                <strong>Result:</strong>
                {{ result.signature_report.message }}
            </div>
            {% else %}
            <p>Signature verification report is not available.</p>
            {% endif %}

            <h3>PDF Tampering Detection Report</h3>

            {% if result.anomaly_report %}
            <div class="field">
                <strong>Risk Level:</strong>
                {{ result.anomaly_report.risk_level }}
            </div>

            <div class="field">
                <strong>Anomaly Count:</strong>
                {{ result.anomaly_report.anomaly_count }}
            </div>

            <div class="field">
                <strong>SHA-256 File Hash:</strong>
                <code>{{ result.anomaly_report.file_sha256 }}</code>
            </div>

            <h4>Checks Performed</h4>
            <ul>
                {% for check_name, check_status in result.anomaly_report.checks.items() %}
                <li><strong>{{ check_name }}:</strong> {{ check_status }}</li>
                {% endfor %}
            </ul>

            <h4>Detected Indicators</h4>
            <ul>
                {% for finding in result.anomaly_report.anomalies %}
                <li>{{ finding }}</li>
                {% else %}
                <li>No anomaly indicators found by the current checks.</li>
                {% endfor %}
            </ul>

            <p class="note">
            These results are preliminary indicators, not proof of forgery.
            Manual verification is required.
            </p>
            {% else %}
            <p>Tampering analysis report is unavailable.</p>
            {% endif %}
        </section>
        {% endif %}
    </main>
</body>
</html>
"""



def analyze_document_anomalies(file_path, document_text):
    import hashlib
    import os
    import re
    from datetime import datetime

    findings = []
    checks = {
        "metadata_check": "Not completed",
        "revision_check": "Not completed",
        "font_check": "Not completed",
        "issue_date_check": "Not completed",
        "active_content_check": "Not completed",
        "file_hash_check": "Not completed"
    }

    file_sha256 = None
    page_count = 0
    metadata = {}

    def add_finding(message):
        if message not in findings:
            findings.append(message)

    def parse_pdf_date(value):
        if not value:
            return None

        match = re.search(
            r"D:(\d{4})(\d{2})?(\d{2})?"
            r"(\d{2})?(\d{2})?(\d{2})?",
            str(value)
        )

        if not match:
            return None

        parts = [
            int(value or 0)
            for value in match.groups()
        ]

        try:
            return datetime(
                parts[0],
                parts[1] or 1,
                parts[2] or 1,
                parts[3],
                parts[4],
                parts[5]
            )
        except ValueError:
            return None

    try:
        # 1. Calculate SHA-256 fingerprint of the uploaded file.
        sha256 = hashlib.sha256()

        with open(file_path, "rb") as uploaded_file:
            for chunk in iter(
                lambda: uploaded_file.read(1024 * 1024),
                b""
            ):
                sha256.update(chunk)

        file_sha256 = sha256.hexdigest()
        checks["file_hash_check"] = "Completed"
        trusted_original_hash = (
            "c132f7708fbf19901876fe7004e005653c4a0cc6059650bde7221600c44823d1"
        )

        if file_sha256 != trusted_original_hash:
            add_finding(
                "File hash differs from the trusted original reference. "
                "The file may have been modified."
            )

        # PDF-specific checks.
        if os.path.splitext(file_path)[1].lower() != ".pdf":
            add_finding(
                "PDF forensic checks were skipped because "
                "the uploaded file is not a PDF."
            )

        else:
            with pymupdf.open(file_path) as pdf:
                page_count = len(pdf)
                metadata = pdf.metadata or {}

                # 2. Check whether the PDF has a readable text layer.
                extracted_pdf_text = "\n".join(
                    page.get_text()
                    for page in pdf
                ).strip()

                if not extracted_pdf_text:
                    add_finding(
                        "No selectable PDF text layer found. "
                        "This may be a scanned document; "
                        "font-based checks are limited."
                    )

                # 3. Inspect creation and modification metadata.
                created_raw = metadata.get("creationDate") or ""
                modified_raw = metadata.get("modDate") or ""

                created = parse_pdf_date(created_raw)
                modified = parse_pdf_date(modified_raw)
                now = datetime.now()

                if not created:
                    add_finding(
                        "Creation timestamp is missing or unreadable."
                    )
                elif created > now:
                    add_finding(
                        "PDF creation timestamp is in the future."
                    )

                if not modified:
                    add_finding(
                        "Modification timestamp is missing or unreadable."
                    )
                elif modified > now:
                    add_finding(
                        "PDF modification timestamp is in the future."
                    )

                if created and modified and modified < created:
                    add_finding(
                        "Modification timestamp is earlier than "
                        "creation timestamp."
                    )

                checks["metadata_check"] = "Completed"

                # 4. Inspect PDF revision indicators.
                with open(file_path, "rb") as pdf_file:
                    pdf_bytes = pdf_file.read()

                eof_count = pdf_bytes.count(b"%%EOF")
                previous_xref_count = pdf_bytes.count(b"/Prev")

                if eof_count > 1 or previous_xref_count > 0:
                    add_finding(
                        "Multiple PDF revision indicators detected. "
                        "This can occur during normal PDF editing; "
                        "manual review is recommended."
                    )

                checks["revision_check"] = "Completed"

                # 5. Check for unusual text font-size variations.
                # This check applies only when selectable text exists.
                font_check_possible = bool(extracted_pdf_text)

                if font_check_possible:
                    font_findings = []

                    for page_number, page in enumerate(pdf, start=1):
                        blocks = page.get_text("dict").get(
                            "blocks", []
                        )
                        font_sizes = []

                        for block in blocks:
                            if block.get("type") != 0:
                                continue

                            for line in block.get("lines", []):
                                for span in line.get("spans", []):
                                    span_text = span.get(
                                        "text", ""
                                    ).strip()
                                    size = span.get("size", 0)

                                    if (
                                        span_text
                                        and isinstance(size, (int, float))
                                        and size > 0
                                    ):
                                        font_sizes.append(float(size))

                        if len(font_sizes) >= 10:
                            sorted_sizes = sorted(font_sizes)
                            median_size = sorted_sizes[
                                len(sorted_sizes) // 2
                            ]

                            unusual_sizes = [
                                size for size in font_sizes
                                if size > median_size * 2.5
                                or size < median_size * 0.4
                            ]

                            if unusual_sizes:
                                font_findings.append(page_number)

                    for page_number in font_findings:
                        add_finding(
                            f"Page {page_number}: unusual font-size "
                            "variation detected. Manual review recommended."
                        )

                    checks["font_check"] = "Completed"
                else:
                    checks["font_check"] = "Not available: scanned PDF"

                # 6. Compare explicitly labelled issue date with PDF
                # modification metadata.
                issue_match = re.search(
                    r"(?:date\s+of\s+issue|issue\s+date|issued\s+on)"
                    r"\s*[:\-]?\s*"
                    r"(\d{4}[-/]\d{1,2}[-/]\d{1,2}"
                    r"|\d{1,2}[-/]\d{1,2}[-/]\d{4})",
                    document_text or "",
                    re.IGNORECASE
                )

                if issue_match:
                    raw_date = issue_match.group(1)
                    issue_date = None

                    for fmt in (
                        "%Y-%m-%d",
                        "%Y/%m/%d",
                        "%d-%m-%Y",
                        "%d/%m/%Y",
                        "%m-%d-%Y",
                        "%m/%d/%Y"
                    ):
                        try:
                            issue_date = datetime.strptime(
                                raw_date, fmt
                            )
                            break
                        except ValueError:
                            continue

                    if issue_date:
                        if issue_date > now:
                            add_finding(
                                "Document issue date is in the future."
                            )

                        if modified and modified.date() > issue_date.date():
                            add_finding(
                                "PDF modification date is after the "
                                "stated issue date. Manual review recommended."
                            )

                        checks["issue_date_check"] = "Completed"
                    else:
                        checks["issue_date_check"] = (
                            "Not completed: issue date could not be parsed"
                        )
                else:
                    checks["issue_date_check"] = (
                        "Not available: explicitly labelled issue date "
                        "was not detected"
                    )

                # 7. Look for PDF active-content indicators.
                # These indicators alone do not prove malicious activity.
                active_patterns = [
                    (b"/JavaScript", "JavaScript"),
                    (b"/JS", "JS action"),
                    (b"/OpenAction", "OpenAction"),
                    (b"/AA", "additional action"),
                    (b"/Launch", "Launch action")
                ]

                detected_active_items = [
                    label for pattern, label in active_patterns
                    if pattern in pdf_bytes
                ]

                if detected_active_items:
                    add_finding(
                        "PDF active-content indicators detected: "
                        + ", ".join(detected_active_items)
                        + ". Inspect the document before trusting it."
                    )

                checks["active_content_check"] = "Completed"

    except Exception:
        app.logger.exception(
            "PDF anomaly analysis failed for %s", file_path
        )
        add_finding(
            "One or more forensic checks could not be completed."
        )

    # These checks identify indicators, not confirmed forgery.
    if any(
        item.startswith("PDF active-content indicators detected")
        for item in findings
    ):
        risk_level = "High Review Priority"
    elif findings:
        risk_level = "Review Required"
    else:
        risk_level = "No Indicators Found"

    return {
        "anomalies": findings,
        "anomaly_count": len(findings),
        "review_required": len(findings) > 0,
        "risk_level": risk_level,
        "checks": checks,
        "file_sha256": file_sha256,
        "page_count": page_count,
        "metadata": {
            "creation_date": metadata.get("creationDate"),
            "modification_date": metadata.get("modDate"),
            "creator": metadata.get("creator"),
            "producer": metadata.get("producer")
        }
    }


# ---------------- ROUTES ----------------

@app.route("/", methods=["GET"])
def home():
    return render_template_string(HTML_PAGE)


@app.route("/dashboard", methods=["GET"])
def dashboard():
    search_id = request.args.get("application_id", "").strip()

    # Get verification records from MongoDB
    query = {}

    if search_id:
        query["application_id"] = {
            "$regex": re.escape(search_id),
            "$options": "i"
        }

    records = list(
        verification_checks.find(
            query,
            {"_id": 0}
        ).sort("checked_at", -1).limit(100)
    )

    # Count all stored verification outcomes
    total = verification_checks.count_documents({})

    verified_count = verification_checks.count_documents({
        "status": "Verified"
    })

    mismatch_count = verification_checks.count_documents({
        "status": "Mismatch Detected"
    })

    review_count = verification_checks.count_documents({
        "status": "Manual Review Required"
    })

    return render_template_string("""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>SmartVerify Dashboard</title>
        <style>
            body {
                font-family: Arial, sans-serif;
                background: #f2f5fa;
                color: #18243a;
                margin: 0;
            }
            header {
                background: #12264a;
                color: white;
                padding: 22px;
                text-align: center;
            }
            main {
                max-width: 1100px;
                margin: 25px auto;
                padding: 0 16px;
            }
            .cards {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
                gap: 15px;
                margin: 20px 0;
            }
            .card {
                background: white;
                border-radius: 10px;
                padding: 20px;
                box-shadow: 0 3px 12px #0000000d;
                overflow-wrap: anywhere;
            }
            .number {
                font-size: 28px;
                font-weight: bold;
                margin-top: 10px;
            }
            form {
                display: flex;
                flex-wrap: wrap;
                gap: 10px;
            }
            input {
                padding: 10px;
                min-width: 220px;
                flex: 1;
            }
            button, .back {
                background: #1768d2;
                color: white;
                padding: 10px 16px;
                border: none;
                border-radius: 6px;
                text-decoration: none;
                cursor: pointer;
            }
            .table-wrap {
                overflow-x: auto;
            }
            table {
                border-collapse: collapse;
                width: 100%;
                background: white;
            }
            th, td {
                padding: 12px;
                border-bottom: 1px solid #ddd;
                text-align: left;
                overflow-wrap: anywhere;
            }
            th {
                background: #e8eef7;
            }
            .note {
                color: #58657a;
                font-size: 13px;
            }
        </style>
    </head>
    <body>
        <header>
            <h1>AICTE SmartVerify</h1>
            <p>Verification History Dashboard</p>
        </header>

        <main>
            <a class="back" href="/">Back to Upload</a>

            <div class="cards">
                <div class="card">
                    Total Checks
                    <div class="number">{{ total }}</div>
                </div>
                <div class="card">
                    Verified
                    <div class="number">{{ verified_count }}</div>
                </div>
                <div class="card">
                    Mismatch Detected
                    <div class="number">{{ mismatch_count }}</div>
                </div>
                <div class="card">
                    Manual Review
                    <div class="number">{{ review_count }}</div>
                </div>
            </div>

            <section class="card">
                <h2>Search Verification History</h2>
                <form method="GET" action="/dashboard">
                    <input
                        type="text"
                        name="application_id"
                        placeholder="Enter application ID"
                        value="{{ search_id }}"
                    >
                    <button type="submit">Search</button>
                    <a class="back" href="/dashboard">Clear</a>
                </form>
            </section>

            <section class="card">
                <h2>Recent Verification Records</h2>

                <div class="table-wrap">
                    <table>
                        <thead>
                            <tr>
                                <th>Verification ID</th>
                                <th>Application ID</th>
                                <th>Document ID</th>
                                <th>Method</th>
                                <th>Status</th>
                                <th>Checked At</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for record in records %}
                            <tr>
                                <td>{{ record.get("verification_id", "-") }}</td>
                                <td>{{ record.get("application_id", "-") }}</td>
                                <td>{{ record.get("document_id", "-") }}</td>
                                <td>{{ record.get("verification_method", "-") }}</td>
                                <td>{{ record.get("status", "-") }}</td>
                                <td>{{ record.get("checked_at", "-") }}</td>
                            </tr>
                            {% else %}
                            <tr>
                                <td colspan="6">
                                    No verification records found.
                                </td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>

                <p class="note">
                    Showing up to 100 records. These are prototype results;
                    final regulatory decisions require authorized human review.
                </p>
            </section>
        </main>
    </body>
    </html>
    """,
    records=records,
    total=total,
    verified_count=verified_count,
    mismatch_count=mismatch_count,
    review_count=review_count,
    search_id=search_id
    )


@app.route("/download-report")
def download_report():
    records = list(
        db["verification_checks"].find(
            {},
            {"_id": 0}
        ).sort("checked_at", -1)
    )

    if not records:
        return "No verification records available to download.", 404

    output = io.StringIO()
    fieldnames = sorted(
        {key for record in records for key in record.keys()}
    )

    writer = csv.DictWriter(
        output,
        fieldnames=fieldnames,
        extrasaction="ignore"
    )
    writer.writeheader()

    for record in records:
        clean_record = {
            key: str(value) for key, value in record.items()
        }
        writer.writerow(clean_record)

    response = app.response_class(
        output.getvalue(),
        mimetype="text/csv",
        headers={
            "Content-Disposition":
                "attachment; filename=verification_report.csv"
        }
    )
    return response

@app.route("/verify", methods=["POST"])
def verify():
    saved_path = None

    try:
        if "document" not in request.files:
            return render_template_string(
                HTML_PAGE,
                error="Please select a document."
            )

        file = request.files["document"]

        if not file or not file.filename:
            return render_template_string(
                HTML_PAGE,
                error="No file was selected."
            )

        if not allowed_file(file.filename):
            return render_template_string(
                HTML_PAGE,
                error="Unsupported file type."
            )

        original_name = secure_filename(file.filename)
        extension = original_name.rsplit(".", 1)[1].lower()

        saved_name = uuid.uuid4().hex + "." + extension
        saved_path = UPLOAD_FOLDER / saved_name

        file.save(saved_path)

        if saved_path.stat().st_size == 0:
            return render_template_string(
                HTML_PAGE,
                error="The uploaded file is empty."
            )

        # Extract document text using OCR/PDF text extraction.
        document_text = extract_document_text(
            str(saved_path),
            extension
        )

        if not document_text:
            return render_template_string(
                HTML_PAGE,
                error="No text extracted. Check OCR installation and image quality."
            )

        # Compare the document against reference records.
        result = verify_document(document_text)
        
        # Extract the signature area from the uploaded PDF.
        signature_crop_path = str(
            saved_path.with_name(
                saved_path.stem + "_signature.png"
            )
        )

        try:
            extract_signature_crop(
                str(saved_path),
                signature_crop_path,
                page_number=0
            )

            # Compare the extracted signature with saved references.
            signature_report = check_duplicate_signature(
                signature_crop_path
            )

        except Exception:
            app.logger.exception("Signature check failed")
            signature_report = {
                "possible_duplicate": False,
                "similarity_score": 0.0,
                "matched_reference_id": None,
                "message": "Signature check could not be completed."
            }

        # Attach signature results to the verification result.
        result["signature_report"] = signature_report

        # Analyze PDF metadata and date anomalies
        anomaly_report = analyze_document_anomalies(
         str(saved_path),
        document_text
)

# Attach anomaly details to the verification result
        result["anomaly_report"] = anomaly_report       

        # MongoDB schema requires one of these status values.
        database_status = {
            "Match": "Verified",
            "Mismatch": "Mismatch Detected",
            "Manual Review": "Manual Review Required"
        }.get(result["status"], "Failed")

        # Required fields in verification_checks collection.
        verification_record = {
            "verification_id": (
                "VER-" + uuid.uuid4().hex[:12].upper()
            ),
            "application_id": (
                result["application_id"]
                if result["application_id"] != "Not detected"
                else "UNKNOWN"
            ),
            "document_id": "DOC-" + uuid.uuid4().hex[:12].upper(),
            "verification_method": "OCR and Reference Database Comparison",
            "status": database_status,
            "checked_at": datetime.now(timezone.utc)
        }

        # Save verification result.
        verification_checks.insert_one(verification_record)
        # Save the detailed verification report to MongoDB.
        validation_report = {
            "report_id": "REP-" + uuid.uuid4().hex[:12].upper(),
            "verification_id": verification_record["verification_id"],
            "application_id": verification_record["application_id"],
            "document_id": verification_record["document_id"],
            "checked_at": datetime.now(timezone.utc),
            "verification_status": verification_record["status"],
            "file_sha256": (
                anomaly_report.get("file_sha256")
                or anomaly_report.get("sha256")
                or anomaly_report.get("hash")
            ),
            "anomaly_count": anomaly_report.get(
                "anomaly_count",
                anomaly_report.get("count", 0)
            ),
            "risk_level": anomaly_report.get(
                "risk_level", "Not available"
            ),
            "anomaly_report": anomaly_report,
            "signature_report": signature_report,
            "report_type": "PDF Verification"
        }

        print("DEBUG: Saving validation report...")
        validation_reports.insert_one(validation_report)
        print("DEBUG: Validation report saved successfully!")
        app.logger.info(
            "Validation report saved: %s",
            validation_report["report_id"]
        )
        return render_template_string(
            HTML_PAGE,
            result=result
        )

    except Exception:
        app.logger.exception("Document verification failed")

        return render_template_string(
            HTML_PAGE,
            error=(
                "Verification failed. Check MongoDB, OCR configuration, "
                "and the terminal for the error details."
            )
        )

    finally:
        # Delete the temporary uploaded file after processing.
        if saved_path and saved_path.exists():
            try:
                saved_path.unlink()
            except OSError:
                app.logger.warning("Temporary upload could not be deleted.")


@app.errorhandler(413)
def file_too_large(error):
    return render_template_string(
        HTML_PAGE,
        error="File is too large. Maximum allowed size is 10 MB."
    ), 413

def test_signature_reference():
    image_path = "uploads/signature_crop.png"

    signature_hash = calculate_signature_hash(image_path)

    print("Signature hash generated successfully!")
    print("Hash length:", len(signature_hash))

    save_signature_reference(
        "SIG-DEMO-001",
        image_path
    )

    print("Signature reference saved to MongoDB.")

# ---------------- START APPLICATION ----------------

if __name__ == "__main__":
    try:
        mongo_client.admin.command("ping")
        print("MongoDB connected successfully!")
        print("Database:", db.name)
    except Exception as error:
        print("MongoDB connection failed:", error)

    app.run(debug=True)

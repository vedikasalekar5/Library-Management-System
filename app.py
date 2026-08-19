import os
import sqlite3
import csv
import secrets
import time
from pathlib import Path
from datetime import date, datetime, timedelta
from functools import wraps
from io import BytesIO, StringIO
from textwrap import wrap
from urllib.parse import quote

import requests
import qrcode
import psycopg2
from psycopg2.extras import RealDictCursor, DictCursor

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*args, **kwargs):
        """Allow the app to run when python-dotenv is unavailable."""
        return False

from backup_database import create_backup_if_due
from aureon_chatbot import answer_student_question
from digital_book_service import (
    delete_private_pdf,
    grant_digital_access,
    member_has_digital_access,
    private_pdf_path,
    save_private_pdf,
)
from whatsapp_service import send_and_log_whatsapp

try:
    import razorpay
except ImportError:
    razorpay = None

from flask import (
    Flask,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    Response,
    send_file,
    session,
    url_for,
)

from reportlab.lib.pagesizes import A4, A5
from reportlab.lib.colors import HexColor, white
from reportlab.pdfgen import canvas
from werkzeug.security import (
    check_password_hash,
    generate_password_hash,
)


# ============================================================
# APPLICATION PATHS AND ENVIRONMENT
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

# Load variables from the local .env file
load_dotenv(BASE_DIR / ".env")

# PostgreSQL database connection
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL environment variable is not configured. "
        "Set DATABASE_URL in .env for local use or Render Environment Variables."
    )

_DATABASE_URL_PLACEHOLDERS = {
    "YOUR_RENDER_EXTERNAL_DATABASE_URL",
    "YOUR_RENDER_INTERNAL_DATABASE_URL",
    "YOUR_DATABASE_URL",
}
if DATABASE_URL in _DATABASE_URL_PLACEHOLDERS or DATABASE_URL.startswith("YOUR_"):
    raise RuntimeError(
        "DATABASE_URL still contains a placeholder. Replace it with your real PostgreSQL URL."
    )


# ============================================================
# FLASK APPLICATION
# ============================================================

app = Flask(__name__)

app.secret_key = os.environ.get(
    "SECRET_KEY",
    "simple-library-secret-key",
)

# Secure session-cookie settings. COOKIE_SECURE should be 1 only on HTTPS.
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = (
    os.environ.get("COOKIE_SECURE", "0").strip() == "1"
)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024

@app.context_processor
def inject_current_year():
    return {
        "current_year": date.today().year
    }

# -------------------------------------------------
# DIGITAL LIBRARY
# -------------------------------------------------

DIGITAL_BOOKS_DIR = os.path.join(
    BASE_DIR,
    "protected_books"
)

os.makedirs(
    DIGITAL_BOOKS_DIR,
    exist_ok=True
)

LIBRARIAN_USERNAME = os.environ.get(
    "LIBRARIAN_USERNAME",
    os.environ.get("ADMIN_USERNAME", "mandar"),
)
LIBRARIAN_PASSWORD = os.environ.get(
    "LIBRARIAN_PASSWORD",
    os.environ.get("ADMIN_PASSWORD", "mandar123"),
)

FINE_AMOUNT = 10
ISSUE_DAYS = 7

CARD_PASSWORD_MIN_LENGTH = 8
CARD_MAX_FAILED_ATTEMPTS = 5
CARD_LOCK_MINUTES = 15
CARD_SESSION_MINUTES = 30


def _environment_integer(
    variable_name,
    default_value,
    minimum_value=1,
    maximum_value=3600,
):
    """Read a bounded positive integer from the environment."""
    try:
        value = int(
            os.environ.get(
                variable_name,
                str(default_value),
            )
        )
    except (TypeError, ValueError):
        value = default_value

    return max(
        minimum_value,
        min(value, maximum_value),
    )


LIBRARIAN_ALLOWED_EMAIL = os.environ.get(
    "LIBRARIAN_ALLOWED_EMAIL",
    "vedikasalekar9@gmail.com",
).strip().lower()

OTP_LENGTH = _environment_integer(
    "OTP_LENGTH",
    6,
    minimum_value=4,
    maximum_value=8,
)
OTP_EXPIRY_MINUTES = _environment_integer(
    "OTP_EXPIRY_MINUTES",
    5,
    minimum_value=1,
    maximum_value=30,
)
OTP_MAX_ATTEMPTS = _environment_integer(
    "OTP_MAX_ATTEMPTS",
    5,
    minimum_value=1,
    maximum_value=10,
)
OTP_RESEND_SECONDS = _environment_integer(
    "OTP_RESEND_SECONDS",
    60,
    minimum_value=15,
    maximum_value=600,
)
OTP_VERIFIED_MINUTES = _environment_integer(
    "OTP_VERIFIED_MINUTES",
    10,
    minimum_value=2,
    maximum_value=60,
)

RAZORPAY_KEY_ID = os.environ.get(
    "RAZORPAY_KEY_ID",
    "",
).strip()

RAZORPAY_KEY_SECRET = os.environ.get(
    "RAZORPAY_KEY_SECRET",
    "",
).strip()


_RAZORPAY_PLACEHOLDER_PARTS = {
    "your_key",
    "your-key",
    "paste",
    "replace",
    "example",
}

_razorpay_key_text = (
    RAZORPAY_KEY_ID + " " + RAZORPAY_KEY_SECRET
).lower()

RAZORPAY_CONFIGURED = (
    razorpay is not None
    and bool(RAZORPAY_KEY_ID)
    and bool(RAZORPAY_KEY_SECRET)
    and not any(
        part in _razorpay_key_text
        for part in _RAZORPAY_PLACEHOLDER_PARTS
    )
)

print(
    "Razorpay configured:",
    RAZORPAY_CONFIGURED,
)

print(
    "Razorpay mode:",
    (
        "TEST"
        if RAZORPAY_CONFIGURED
        and RAZORPAY_KEY_ID.startswith("rzp_test_")
        else "LIVE"
        if RAZORPAY_CONFIGURED
        and RAZORPAY_KEY_ID.startswith("rzp_live_")
        else "NOT CONFIGURED"
    ),
)


def get_razorpay_client():
    """Return a configured Razorpay client or None."""
    if not RAZORPAY_CONFIGURED:
        return None

    return razorpay.Client(
        auth=(
            RAZORPAY_KEY_ID,
            RAZORPAY_KEY_SECRET,
        )
    )


# -------------------------------------------------
# DATABASE
# -------------------------------------------------

class _PostgresCursor:
    """Compatibility wrapper for the existing SQLite-style query code."""

    def __init__(self, cursor):
        self._cursor = cursor

    @staticmethod
    def _translate(query):
        query = query.replace("?", "%s")
        query = query.replace("datetime('now', 'localtime')", "CURRENT_TIMESTAMP")
        query = query.replace('datetime("now", "localtime")', "CURRENT_TIMESTAMP")
        query = query.replace("date('now', 'localtime')", "CURRENT_DATE")
        query = query.replace('date("now", "localtime")', "CURRENT_DATE")
        if "INSERT OR IGNORE INTO" in query.upper():
            query = query.replace("INSERT OR IGNORE INTO", "INSERT INTO")
            query = query.replace("insert or ignore into", "insert into")
            query = query.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
        return query

    def execute(self, query, params=None):
        self._cursor.execute(self._translate(query), params)
        return self

    def executemany(self, query, params_list):
        self._cursor.executemany(self._translate(query), params_list)
        return self

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    @property
    def rowcount(self):
        return self._cursor.rowcount

    @property
    def lastrowid(self):
        self._cursor.execute("SELECT LASTVAL()")
        row = self._cursor.fetchone()
        return row[0] if row else None


class _PostgresConnection:
    """Compatibility layer that lets Aureon keep its SQLite-style DB calls on PostgreSQL."""

    def __init__(self, dsn):
        self._connection = psycopg2.connect(dsn)

    @staticmethod
    def _translate_script(script):
        script = script.replace("?", "%s")
        script = script.replace("datetime('now', 'localtime')", "CURRENT_TIMESTAMP")
        script = script.replace('datetime("now", "localtime")', "CURRENT_TIMESTAMP")
        script = script.replace("date('now', 'localtime')", "CURRENT_DATE")
        script = script.replace('date("now", "localtime")', "CURRENT_DATE")
        script = script.replace(
            "INTEGER PRIMARY KEY AUTOINCREMENT",
            "INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY",
        )
        return script

    def execute(self, query, params=None):
        cursor = self._connection.cursor(cursor_factory=DictCursor)
        return _PostgresCursor(cursor).execute(query, params)

    def executemany(self, query, params_list):
        cursor = self._connection.cursor(cursor_factory=DictCursor)
        return _PostgresCursor(cursor).executemany(query, params_list)

    def executescript(self, script):
        """Execute the existing schema script using PostgreSQL syntax."""
        script = self._translate_script(script)
        cursor = self._connection.cursor(cursor_factory=DictCursor)
        for statement in script.split(";"):
            statement = statement.strip()
            if not statement:
                continue
            if statement.upper().startswith("INSERT OR IGNORE INTO"):
                statement = statement.replace("INSERT OR IGNORE INTO", "INSERT INTO", 1)
                statement += " ON CONFLICT DO NOTHING"
            cursor.execute(statement)
        cursor.close()

    def commit(self):
        self._connection.commit()

    def rollback(self):
        self._connection.rollback()

    def close(self):
        self._connection.close()


def get_db_connection():
    return _PostgresConnection(DATABASE_URL)


def _column_names(connection, table_name):
    """Return PostgreSQL column names for a table."""
    rows = connection.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = ?
        """,
        (table_name,),
    ).fetchall()
    return {row[0] for row in rows}


def _add_column_if_missing(
    connection,
    table_name,
    column_name,
    column_definition,
):
    columns = _column_names(connection, table_name)
    if column_name not in columns:
        connection.execute(
            f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}"
        )


def clean_isbn(isbn):
    """Remove spaces and hyphens from an ISBN."""
    return (
        (isbn or "")
        .replace("-", "")
        .replace(" ", "")
        .strip()
        .upper()
    )


def is_valid_isbn_format(isbn):
    """Check the basic ISBN-10 or ISBN-13 format."""
    if len(isbn) == 13:
        return isbn.isdigit()

    if len(isbn) == 10:
        return (
            isbn[:9].isdigit()
            and (
                isbn[9].isdigit()
                or isbn[9] == "X"
            )
        )

    return False



def _prepare_whatsapp_phone(phone):
    """Return a WhatsApp wa.me phone number without +, spaces or symbols."""
    digits = "".join(
        character
        for character in str(phone or "")
        if character.isdigit()
    )

    if digits.startswith("00"):
        digits = digits[2:]

    # Indian local mobile number: 9876543210 -> 919876543210
    if len(digits) == 10:
        digits = "91" + digits
    elif len(digits) == 11 and digits.startswith("0"):
        digits = "91" + digits[1:]

    if not 10 <= len(digits) <= 15:
        return None

    return digits


def create_database():
    connection = get_db_connection()

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS books (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            isbn TEXT,
            title TEXT NOT NULL,
            author TEXT NOT NULL,
            publisher TEXT,
            published_year TEXT,
            category TEXT,
            description TEXT,
            cover_url TEXT,
            total_copies INTEGER NOT NULL DEFAULT 1,
            available_copies INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            membership_id TEXT,
            name TEXT NOT NULL,
            email TEXT,
            phone TEXT,
            enrollment_no TEXT,
            department TEXT,
            study_year TEXT,
            qr_token TEXT
        );

        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            book_id INTEGER NOT NULL,
            member_id INTEGER NOT NULL,
            issue_date TEXT NOT NULL,
            due_date TEXT NOT NULL,
            return_date TEXT,
            status TEXT NOT NULL DEFAULT 'Issued',
            fine INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (book_id) REFERENCES books(id),
            FOREIGN KEY (member_id) REFERENCES members(id)
        );

        CREATE TABLE IF NOT EXISTS fine_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transaction_id INTEGER NOT NULL UNIQUE,
            receipt_no TEXT NOT NULL UNIQUE,
            amount INTEGER NOT NULL,
            payment_method TEXT NOT NULL,
            payment_reference TEXT,
            paid_at TEXT NOT NULL,
            FOREIGN KEY (transaction_id)
                REFERENCES transactions(id)
        );

        CREATE TABLE IF NOT EXISTS book_incidents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transaction_id INTEGER NOT NULL,
            member_id INTEGER NOT NULL,
            book_id INTEGER NOT NULL,
            incident_type TEXT NOT NULL,
            description TEXT,
            noticed_date TEXT,
            charge INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'Pending',
            reported_at TEXT NOT NULL,
            reviewed_at TEXT,
            reviewed_by TEXT,
            admin_note TEXT,
            FOREIGN KEY (transaction_id)
                REFERENCES transactions(id),
            FOREIGN KEY (member_id)
                REFERENCES members(id),
            FOREIGN KEY (book_id)
                REFERENCES books(id)
        );

        CREATE TABLE IF NOT EXISTS incident_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            incident_id INTEGER NOT NULL UNIQUE,
            receipt_no TEXT UNIQUE,
            amount INTEGER NOT NULL,
            payment_method TEXT NOT NULL,
            payment_status TEXT NOT NULL DEFAULT 'Pending',
            payment_reference TEXT,
            gateway_order_id TEXT UNIQUE,
            gateway_payment_id TEXT UNIQUE,
            gateway_signature TEXT,
            cash_requested_at TEXT,
            cash_confirmed_by TEXT,
            paid_at TEXT,
            FOREIGN KEY (incident_id)
                REFERENCES book_incidents(id)
        );


        CREATE TABLE IF NOT EXISTS digital_books (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            title TEXT NOT NULL,

            author TEXT NOT NULL,

            category TEXT,

            description TEXT,

            cover_url TEXT,

            file_name TEXT NOT NULL,

            read_price INTEGER NOT NULL DEFAULT 10,

            download_price INTEGER NOT NULL DEFAULT 100,

            is_active INTEGER NOT NULL DEFAULT 1,

            created_at TEXT NOT NULL
        );


        CREATE TABLE IF NOT EXISTS digital_purchases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            member_id INTEGER NOT NULL,

            digital_book_id INTEGER NOT NULL,

            purchase_type TEXT NOT NULL,

            amount INTEGER NOT NULL,

            payment_status TEXT NOT NULL DEFAULT 'Pending',

            gateway_order_id TEXT UNIQUE,

            gateway_payment_id TEXT UNIQUE,

            gateway_signature TEXT,

            purchased_at TEXT,

            FOREIGN KEY (member_id)
                REFERENCES members(id),

            FOREIGN KEY (digital_book_id)
                REFERENCES digital_books(id),

            UNIQUE (
                member_id,
                digital_book_id,
                purchase_type
            )
        );
        """
    )


    

    # Student Digital Card, notification and fine-request tables.
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER,
            recipient_type TEXT NOT NULL DEFAULT 'member',
            category TEXT NOT NULL DEFAULT 'General',
            priority TEXT NOT NULL DEFAULT 'Normal',
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT,
            created_by TEXT,
            unique_key TEXT UNIQUE,
            is_active INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY (member_id) REFERENCES members(id)
        );

        CREATE TABLE IF NOT EXISTS notification_reads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            notification_id INTEGER NOT NULL,
            member_id INTEGER NOT NULL,
            read_at TEXT NOT NULL,
            UNIQUE (notification_id, member_id),
            FOREIGN KEY (notification_id) REFERENCES notifications(id),
            FOREIGN KEY (member_id) REFERENCES members(id)
        );

        CREATE TABLE IF NOT EXISTS fine_payment_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transaction_id INTEGER NOT NULL UNIQUE,
            member_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            payment_method TEXT NOT NULL,
            payment_status TEXT NOT NULL DEFAULT 'Pending',
            payment_reference TEXT,
            gateway_order_id TEXT UNIQUE,
            gateway_payment_id TEXT UNIQUE,
            gateway_signature TEXT,
            cash_requested_at TEXT,
            cash_confirmed_by TEXT,
            paid_at TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (transaction_id) REFERENCES transactions(id),
            FOREIGN KEY (member_id) REFERENCES members(id)
        );

        CREATE TABLE IF NOT EXISTS whatsapp_message_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL,
            transaction_id INTEGER,
            notification_type TEXT NOT NULL,
            unique_key TEXT NOT NULL UNIQUE,
            phone TEXT NOT NULL,
            template_name TEXT NOT NULL,
            message_id TEXT,
            delivery_status TEXT NOT NULL,
            error_message TEXT,
            sent_at TEXT NOT NULL,
            FOREIGN KEY (member_id) REFERENCES members(id),
            FOREIGN KEY (transaction_id) REFERENCES transactions(id)
        );

        CREATE TABLE IF NOT EXISTS digital_books (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            book_id INTEGER NOT NULL UNIQUE,
            pdf_filename TEXT NOT NULL,
            read_price INTEGER NOT NULL DEFAULT 10,
            download_price INTEGER NOT NULL DEFAULT 100,
            is_active INTEGER NOT NULL DEFAULT 1,
            uploaded_at TEXT NOT NULL,
            updated_at TEXT,
            FOREIGN KEY (book_id) REFERENCES books(id)
        );

        CREATE TABLE IF NOT EXISTS digital_book_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL,
            digital_book_id INTEGER NOT NULL,
            access_type TEXT NOT NULL,
            amount INTEGER NOT NULL,
            payment_status TEXT NOT NULL DEFAULT 'Pending',
            gateway_order_id TEXT UNIQUE,
            gateway_payment_id TEXT UNIQUE,
            gateway_signature TEXT,
            created_at TEXT NOT NULL,
            paid_at TEXT,
            FOREIGN KEY (member_id) REFERENCES members(id),
            FOREIGN KEY (digital_book_id) REFERENCES digital_books(id)
        );

        CREATE TABLE IF NOT EXISTS digital_book_access (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL,
            digital_book_id INTEGER NOT NULL,
            payment_id INTEGER NOT NULL,
            access_type TEXT NOT NULL,
            granted_at TEXT NOT NULL,
            expires_at TEXT,
            FOREIGN KEY (member_id) REFERENCES members(id),
            FOREIGN KEY (digital_book_id) REFERENCES digital_books(id),
            FOREIGN KEY (payment_id) REFERENCES digital_book_payments(id)
        );

        CREATE TABLE IF NOT EXISTS librarian_otp_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            otp_hash TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 5,
            is_used INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            verified_at TEXT,
            request_ip TEXT,
            user_agent TEXT
        );
        """
    )

    # Safe migrations for databases created by older versions.
    _add_column_if_missing(
        connection,
        "transactions",
        "fine",
        "INTEGER NOT NULL DEFAULT 0",
    )

    _add_column_if_missing(
        connection,
        "members",
        "membership_id",
        "TEXT",
    )
    _add_column_if_missing(
        connection,
        "members",
        "qr_token",
        "TEXT",
    )
    _add_column_if_missing(
        connection,
        "members",
        "enrollment_no",
        "TEXT",
    )
    _add_column_if_missing(
        connection,
        "members",
        "department",
        "TEXT",
    )
    _add_column_if_missing(
        connection,
        "members",
        "study_year",
        "TEXT",
    )
    _add_column_if_missing(
        connection,
        "members",
        "whatsapp_opt_in",
        "INTEGER NOT NULL DEFAULT 0",
    )

    # Secure Digital Card fields for student accounts.
    _add_column_if_missing(
        connection,
        "members",
        "card_password_hash",
        "TEXT",
    )
    _add_column_if_missing(
        connection,
        "members",
        "card_password_created_at",
        "TEXT",
    )
    _add_column_if_missing(
        connection,
        "members",
        "card_password_updated_at",
        "TEXT",
    )
    _add_column_if_missing(
        connection,
        "members",
        "card_failed_attempts",
        "INTEGER NOT NULL DEFAULT 0",
    )
    _add_column_if_missing(
        connection,
        "members",
        "card_locked_until",
        "TEXT",
    )
    _add_column_if_missing(
        connection,
        "members",
        "card_last_login",
        "TEXT",
    )
    _add_column_if_missing(
        connection,
        "members",
        "card_portal_enabled",
        "INTEGER NOT NULL DEFAULT 1",
    )
    _add_column_if_missing(
        connection,
        "members",
        "card_reset_required",
        "INTEGER NOT NULL DEFAULT 0",
    )
    _add_column_if_missing(
        connection,
        "members",
        "card_session_version",
        "INTEGER NOT NULL DEFAULT 1",
    )

    # ISBN Auto-Fill columns for existing databases.
    _add_column_if_missing(
        connection,
        "books",
        "isbn",
        "TEXT",
    )
    _add_column_if_missing(
        connection,
        "books",
        "publisher",
        "TEXT",
    )
    _add_column_if_missing(
        connection,
        "books",
        "published_year",
        "TEXT",
    )
    _add_column_if_missing(
        connection,
        "books",
        "description",
        "TEXT",
    )
    _add_column_if_missing(
        connection,
        "books",
        "cover_url",
        "TEXT",
    )

    _add_column_if_missing(
        connection,
        "fine_payments",
        "payment_reference",
        "TEXT",
    )

    # Lost/Damaged incident columns for older databases.
    _add_column_if_missing(
        connection,
        "book_incidents",
        "description",
        "TEXT",
    )
    _add_column_if_missing(
        connection,
        "book_incidents",
        "noticed_date",
        "TEXT",
    )
    _add_column_if_missing(
        connection,
        "book_incidents",
        "charge",
        "INTEGER NOT NULL DEFAULT 0",
    )
    _add_column_if_missing(
        connection,
        "book_incidents",
        "status",
        "TEXT NOT NULL DEFAULT 'Pending'",
    )
    _add_column_if_missing(
        connection,
        "book_incidents",
        "reported_at",
        "TEXT",
    )
    _add_column_if_missing(
        connection,
        "book_incidents",
        "reviewed_at",
        "TEXT",
    )
    _add_column_if_missing(
        connection,
        "book_incidents",
        "reviewed_by",
        "TEXT",
    )
    _add_column_if_missing(
        connection,
        "book_incidents",
        "admin_note",
        "TEXT",
    )

    # Lost/Damaged payment columns for older databases.
    _add_column_if_missing(
        connection,
        "incident_payments",
        "receipt_no",
        "TEXT",
    )
    _add_column_if_missing(
        connection,
        "incident_payments",
        "amount",
        "INTEGER NOT NULL DEFAULT 0",
    )
    _add_column_if_missing(
        connection,
        "incident_payments",
        "payment_method",
        "TEXT",
    )
    _add_column_if_missing(
        connection,
        "incident_payments",
        "payment_status",
        "TEXT NOT NULL DEFAULT 'Pending'",
    )
    _add_column_if_missing(
        connection,
        "incident_payments",
        "payment_reference",
        "TEXT",
    )
    _add_column_if_missing(
        connection,
        "incident_payments",
        "gateway_order_id",
        "TEXT",
    )
    _add_column_if_missing(
        connection,
        "incident_payments",
        "gateway_payment_id",
        "TEXT",
    )
    _add_column_if_missing(
        connection,
        "incident_payments",
        "gateway_signature",
        "TEXT",
    )
    _add_column_if_missing(
        connection,
        "incident_payments",
        "cash_requested_at",
        "TEXT",
    )
    _add_column_if_missing(
        connection,
        "incident_payments",
        "cash_confirmed_by",
        "TEXT",
    )
    _add_column_if_missing(
        connection,
        "incident_payments",
        "paid_at",
        "TEXT",
    )

    # Recalculate fines for previously returned transactions.
    connection.execute(
        """
        UPDATE transactions
        SET fine = CASE
            WHEN return_date IS NOT NULL
             AND date(return_date) > date(due_date)
            THEN ?
            ELSE 0
        END
        WHERE status = 'Returned'
        """,
        (FINE_AMOUNT,),
    )

    # Generate membership IDs for old members.
    members_without_id = connection.execute(
        """
        SELECT id
        FROM members
        WHERE membership_id IS NULL
           OR TRIM(membership_id) = ''
        """
    ).fetchall()

    for member_row in members_without_id:
        connection.execute(
            """
            UPDATE members
            SET membership_id = ?
            WHERE id = ?
            """,
            (
                f"AUR-{member_row['id']:05d}",
                member_row["id"],
            ),
        )

    # Do not prevent startup if an old database has duplicate IDs.
    try:
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
            idx_members_membership_id
            ON members(membership_id)
            """
        )
    except psycopg2.IntegrityError:
        pass

    # Generate a secure QR token for every existing member.
    members_without_token = connection.execute(
        """
        SELECT id
        FROM members
        WHERE qr_token IS NULL
           OR TRIM(qr_token) = ''
        """
    ).fetchall()

    for member_row in members_without_token:
        connection.execute(
            """
            UPDATE members
            SET qr_token = ?
            WHERE id = ?
            """,
            (
                secrets.token_urlsafe(24),
                member_row["id"],
            ),
        )

    try:
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
            idx_members_qr_token
            ON members(qr_token)
            """
        )
    except psycopg2.IntegrityError:
        # Extremely unlikely, but regenerate duplicate tokens safely.
        duplicate_rows = connection.execute(
            """
            SELECT id
            FROM members
            ORDER BY id
            """
        ).fetchall()

        for member_row in duplicate_rows:
            connection.execute(
                """
                UPDATE members
                SET qr_token = ?
                WHERE id = ?
                """,
                (
                    secrets.token_urlsafe(24),
                    member_row["id"],
                ),
            )

        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
            idx_members_qr_token
            ON members(qr_token)
            """
        )

    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_book_incidents_member
        ON book_incidents(member_id)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_book_incidents_transaction
        ON book_incidents(transaction_id)
        """
    )

    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_notifications_member
        ON notifications(member_id)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_notification_reads_member
        ON notification_reads(member_id)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_fine_payment_requests_member
        ON fine_payment_requests(member_id)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_whatsapp_message_member
        ON whatsapp_message_log(member_id)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_digital_book_access_member
        ON digital_book_access(member_id, digital_book_id)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_digital_book_payments_member
        ON digital_book_payments(member_id, digital_book_id)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_librarian_otp_email_created
        ON librarian_otp_requests(email, created_at)
        """
    )

    connection.commit()
    connection.close()


# -------------------------------------------------
# LOGIN PROTECTION
# -------------------------------------------------

def login_required(view_function):
    """Allow only the logged-in Librarian to open protected pages."""
    @wraps(view_function)
    def wrapped_view(*args, **kwargs):
        if (
            "username" not in session
            or session.get("role") != "librarian"
        ):
            session.clear()

            if request.path.startswith("/api/"):
                return jsonify({
                    "success": False,
                "message": "Your Librarian session has expired. Please log in again.",
                    "login_required": True,
                }), 401

            flash(
                "Please log in to access the Librarian Dashboard.",
                "warning",
            )
            return redirect(url_for("login"))

        return view_function(*args, **kwargs)

    return wrapped_view


def _password_is_strong(password):
    """Return True when a student password meets the portal rules."""
    return (
        len(password or "") >= CARD_PASSWORD_MIN_LENGTH
        and any(character.isupper() for character in password)
        and any(character.islower() for character in password)
        and any(character.isdigit() for character in password)
    )


def _parse_local_datetime(value):
    """Parse an SQLite/local ISO timestamp safely."""
    if not value:
        return None

    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _member_session_is_valid(member, qr_token=None):
    """Validate the signed Flask session for one Digital Card member."""
    if member is None:
        return False

    if session.get("role") != "member":
        return False

    if session.get("member_id") != member["id"]:
        return False

    if qr_token and session.get("member_qr_token") != qr_token:
        return False

    if (
        session.get("card_session_version")
        != member["card_session_version"]
    ):
        return False

    last_activity = session.get("card_last_activity")

    try:
        elapsed_seconds = time.time() - float(last_activity)
    except (TypeError, ValueError):
        return False

    return elapsed_seconds <= CARD_SESSION_MINUTES * 60


def member_card_required(view_function):
    """Allow only a logged-in student to open personal portal routes."""
    @wraps(view_function)
    def wrapped_view(*args, **kwargs):
        member_id = session.get("member_id")
        qr_token = session.get("member_qr_token")

        connection = get_db_connection()
        member = connection.execute(
            """
            SELECT *
            FROM members
            WHERE id = ?
            """,
            (member_id,),
        ).fetchone()
        connection.close()

        if (
            member is None
            or not _member_session_is_valid(member, qr_token)
            or not member["card_portal_enabled"]
        ):
            remembered_token = qr_token
            session.clear()

            flash(
                "Your Digital Card session expired. Please log in again.",
                "warning",
            )

            if remembered_token:
                return redirect(
                    url_for(
                        "member_card_login",
                        qr_token=remembered_token,
                    )
                )

            return redirect(url_for("login"))

        session["card_last_activity"] = time.time()
        return view_function(*args, **kwargs)

    return wrapped_view


def _create_member_notification(
    connection,
    member_id,
    category,
    title,
    message,
    priority="Normal",
    unique_key=None,
    created_by="System",
    expires_at=None,
):
    """Create one student notification without duplicating unique events."""
    try:
        connection.execute(
            """
            INSERT OR IGNORE INTO notifications (
                member_id,
                recipient_type,
                category,
                priority,
                title,
                message,
                created_at,
                expires_at,
                created_by,
                unique_key,
                is_active
            )
            VALUES (
                ?, 'member', ?, ?, ?, ?,
                datetime('now', 'localtime'),
                ?, ?, ?, 1
            )
            """,
            (
                member_id,
                category,
                priority,
                title,
                message,
                expires_at,
                created_by,
                unique_key,
            ),
        )
    except psycopg2.Error as error:
        print("Notification creation error:", error)


def _send_transaction_whatsapp(
    connection,
    transaction_id,
    event_type,
):
    """Send automatic issue/return WhatsApp templates without breaking the app."""
    try:
        row = connection.execute(
            """
            SELECT
                transactions.id AS transaction_id,
                transactions.issue_date,
                transactions.due_date,
                transactions.return_date,
                transactions.fine,
                books.title,
                members.*
            FROM transactions
            JOIN books
              ON books.id = transactions.book_id
            JOIN members
              ON members.id = transactions.member_id
            WHERE transactions.id = ?
            """,
            (transaction_id,),
        ).fetchone()

        if row is None:
            return

        if event_type == "issued":
            send_and_log_whatsapp(
                connection,
                row,
                notification_type="Book Issued",
                unique_key=f"issued:{transaction_id}",
                template_name=os.environ.get(
                    "WHATSAPP_TEMPLATE_BOOK_ISSUED",
                    "rsiet_book_issued",
                ).strip(),
                body_parameters=[
                    row["name"],
                    row["title"],
                    row["issue_date"],
                    row["due_date"],
                ],
                transaction_id=transaction_id,
            )

        elif event_type == "returned":
            send_and_log_whatsapp(
                connection,
                row,
                notification_type="Book Returned",
                unique_key=f"returned:{transaction_id}",
                template_name=os.environ.get(
                    "WHATSAPP_TEMPLATE_BOOK_RETURNED",
                    "rsiet_book_returned",
                ).strip(),
                body_parameters=[
                    row["name"],
                    row["title"],
                    row["return_date"] or "-",
                    str(row["fine"] or 0),
                ],
                transaction_id=transaction_id,
            )

    except Exception as error:
        app.logger.warning(
            "Automatic transaction WhatsApp failed: %s",
            error,
        )


def _send_member_payment_whatsapp(
    connection,
    member_id,
    amount,
    description,
    unique_key,
):
    """Send an automatic payment confirmation template."""
    try:
        member = connection.execute(
            """
            SELECT *
            FROM members
            WHERE id = ?
            """,
            (member_id,),
        ).fetchone()

        send_and_log_whatsapp(
            connection,
            member,
            notification_type="Payment Confirmed",
            unique_key=unique_key,
            template_name=os.environ.get(
                "WHATSAPP_TEMPLATE_PAYMENT_CONFIRMED",
                "rsiet_payment_confirmed",
            ).strip(),
            body_parameters=[
                member["name"] if member else "Student",
                str(amount),
                description,
            ],
        )
    except Exception as error:
        app.logger.warning(
            "Automatic payment WhatsApp failed: %s",
            error,
        )


def _send_incident_whatsapp(
    connection,
    incident_id,
    status,
):
    """Send an automatic lost/damaged report update template."""
    try:
        row = connection.execute(
            """
            SELECT
                book_incidents.id AS incident_id,
                book_incidents.status,
                book_incidents.charge,
                books.title,
                members.*
            FROM book_incidents
            JOIN books
              ON books.id = book_incidents.book_id
            JOIN members
              ON members.id = book_incidents.member_id
            WHERE book_incidents.id = ?
            """,
            (incident_id,),
        ).fetchone()

        if row is None:
            return

        send_and_log_whatsapp(
            connection,
            row,
            notification_type="Incident Update",
            unique_key=f"incident:{incident_id}:{status.lower()}",
            template_name=os.environ.get(
                "WHATSAPP_TEMPLATE_INCIDENT_UPDATE",
                "rsiet_incident_update",
            ).strip(),
            body_parameters=[
                row["name"],
                row["title"],
                status,
                str(row["charge"] or 0),
            ],
        )
    except Exception as error:
        app.logger.warning(
            "Automatic incident WhatsApp failed: %s",
            error,
        )


def _sync_member_automatic_notifications(connection, member_id):
    """Generate issue, return, due, incident and payment messages."""
    today = date.today()

    transaction_rows = connection.execute(
        """
        SELECT
            transactions.id,
            transactions.issue_date,
            transactions.due_date,
            transactions.return_date,
            transactions.status,
            transactions.fine,
            books.title
        FROM transactions
        JOIN books
          ON books.id = transactions.book_id
        WHERE transactions.member_id = ?
        ORDER BY transactions.id DESC
        """,
        (member_id,),
    ).fetchall()

    for transaction in transaction_rows:
        transaction_id = transaction["id"]
        book_title = transaction["title"]

        _create_member_notification(
            connection,
            member_id,
            "Book Issue",
            "Book issued successfully",
            (
                f"{book_title} was issued on "
                f"{transaction['issue_date']} and is due on "
                f"{transaction['due_date']}."
            ),
            unique_key=f"transaction:{transaction_id}:issued",
        )

        if transaction["status"] == "Returned":
            message = (
                f"{book_title} was returned on "
                f"{transaction['return_date']}."
            )

            if transaction["fine"] > 0:
                message += (
                    f" A late-return penalty of "
                    f"₹{transaction['fine']} was added."
                )

            _create_member_notification(
                connection,
                member_id,
                "Book Return",
                "Book return recorded",
                message,
                unique_key=f"transaction:{transaction_id}:returned",
            )

        if transaction["status"] != "Issued":
            continue

        try:
            due_date = date.fromisoformat(transaction["due_date"])
        except (TypeError, ValueError):
            continue

        days_remaining = (due_date - today).days

        if days_remaining == 2:
            _create_member_notification(
                connection,
                member_id,
                "Due Date",
                "Book due in two days",
                (
                    f"{book_title} is due on "
                    f"{transaction['due_date']}."
                ),
                priority="Important",
                unique_key=f"transaction:{transaction_id}:due-two-days",
            )
        elif days_remaining == 0:
            _create_member_notification(
                connection,
                member_id,
                "Due Date",
                "Book is due today",
                (
                    f"{book_title} must be returned today "
                    "to avoid a late-return penalty."
                ),
                priority="Urgent",
                unique_key=f"transaction:{transaction_id}:due-today",
            )
        elif days_remaining < 0:
            _create_member_notification(
                connection,
                member_id,
                "Overdue",
                "Book is overdue",
                (
                    f"{book_title} is overdue by "
                    f"{abs(days_remaining)} day(s). "
                    "Please return it to the Librarian."
                ),
                priority="Urgent",
                unique_key=f"transaction:{transaction_id}:overdue",
            )

    incident_rows = connection.execute(
        """
        SELECT
            book_incidents.id,
            book_incidents.incident_type,
            book_incidents.status,
            book_incidents.charge,
            book_incidents.admin_note,
            books.title
        FROM book_incidents
        JOIN books
          ON books.id = book_incidents.book_id
        WHERE book_incidents.member_id = ?
        """,
        (member_id,),
    ).fetchall()

    for incident in incident_rows:
        status = incident["status"]
        message = (
            f"Your {incident['incident_type'].lower()} report for "
            f"{incident['title']} is currently {status}."
        )

        if status in {"Approved", "Paid"} and incident["charge"] > 0:
            message += f" Charge: ₹{incident['charge']}."

        if incident["admin_note"]:
            message += f" Librarian note: {incident['admin_note']}"

        _create_member_notification(
            connection,
            member_id,
            "Lost or Damaged",
            f"Incident report {status.lower()}",
            message,
            priority=(
                "Important"
                if status in {"Approved", "Rejected"}
                else "Normal"
            ),
            unique_key=f"incident:{incident['id']}:{status}",
        )

    paid_fines = connection.execute(
        """
        SELECT
            fine_payments.transaction_id,
            fine_payments.amount,
            fine_payments.receipt_no,
            books.title
        FROM fine_payments
        JOIN transactions
          ON transactions.id = fine_payments.transaction_id
        JOIN books
          ON books.id = transactions.book_id
        WHERE transactions.member_id = ?
        """,
        (member_id,),
    ).fetchall()

    for payment in paid_fines:
        _create_member_notification(
            connection,
            member_id,
            "Payment",
            "Fine payment completed",
            (
                f"Payment of ₹{payment['amount']} for "
                f"{payment['title']} was completed. "
                f"Receipt: {payment['receipt_no']}."
            ),
            unique_key=(
                f"fine-payment:{payment['transaction_id']}"
            ),
        )


def _get_member_unread_notification_count(connection, member_id):
    return connection.execute(
        """
        SELECT COUNT(*)
        FROM notifications
        LEFT JOIN notification_reads
          ON notification_reads.notification_id = notifications.id
         AND notification_reads.member_id = ?
        WHERE notifications.is_active = 1
          AND (
                notifications.member_id = ?
                OR notifications.recipient_type = 'all'
              )
          AND (
                notifications.expires_at IS NULL
                OR TRIM(notifications.expires_at) = ''
                OR date(notifications.expires_at) >= date('now', 'localtime')
              )
          AND notification_reads.id IS NULL
        """,
        (
            member_id,
            member_id,
        ),
    ).fetchone()[0]


@app.before_request
def protect_secure_scan_actions():
    """
    A QR token identifies the card, but a student password authorizes access.
    Keep only the first scan, login and setup pages public.
    """
    protected_member_endpoints = {
        "pay_qr_fine",
        "qr_fine_receipt",
        "download_qr_fine_receipt",
    }

    if request.endpoint in protected_member_endpoints:
        member_id = (
            request.view_args.get("member_id")
            if request.view_args
            else None
        )

        connection = get_db_connection()
        member = connection.execute(
            """
            SELECT *
            FROM members
            WHERE id = ?
            """,
            (member_id,),
        ).fetchone()
        connection.close()

        if (
            member is not None
            and _member_session_is_valid(
                member,
                member["qr_token"],
            )
        ):
            session["card_last_activity"] = time.time()
            return None

        flash(
            "Please log in to open this Digital Library Card.",
            "warning",
        )

        if member is not None:
            return redirect(
                url_for(
                    "member_card_login",
                    qr_token=member["qr_token"],
                )
            )

        return redirect(url_for("login"))

    if not request.path.startswith("/scan/"):
        return None

    if request.endpoint in {
        "member_scan",
        "member_card_login",
        "member_card_setup",
    }:
        return None

    qr_token = (
        request.view_args.get("qr_token")
        if request.view_args
        else None
    )

    if not qr_token:
        return None

    connection = get_db_connection()
    member = get_member_by_qr_token(
        connection,
        qr_token,
    )
    connection.close()

    if member is not None and _member_session_is_valid(
        member,
        qr_token,
    ):
        session["card_last_activity"] = time.time()
        return None

    if request.is_json or request.endpoint in {
        "create_incident_order",
        "verify_incident_payment",
    }:
        return jsonify({
            "success": False,
            "message": "Student login is required.",
        }), 401

    flash(
        "Please log in to open this Digital Library Card.",
        "warning",
    )
    return redirect(
        url_for(
            "member_card_login",
            qr_token=qr_token,
        )
    )


# -------------------------------------------------
# LIBRARIAN EMAIL OTP SECURITY / LOGIN
# -------------------------------------------------

def _mask_email(email):
    """Hide most characters while keeping the email recognisable."""
    email = str(email or "").strip()

    if "@" not in email:
        return email

    local_part, domain = email.split("@", 1)

    if len(local_part) <= 2:
        masked_local = local_part[:1] + "*"
    else:
        masked_local = (
            local_part[:2]
            + "*" * max(len(local_part) - 3, 3)
            + local_part[-1:]
        )

    return f"{masked_local}@{domain}"


def _clear_librarian_otp_session():
    """Remove temporary OTP information without touching student data."""
    for key in (
        "pending_librarian_otp_id",
        "pending_librarian_otp_email",
        "librarian_otp_verified_email",
        "librarian_otp_verified_until",
    ):
        session.pop(key, None)


def _librarian_otp_session_is_valid():
    """Return True while the verified OTP gate is still active."""
    verified_email = str(
        session.get(
            "librarian_otp_verified_email",
            "",
        )
    ).strip().lower()

    if not secrets.compare_digest(
        verified_email,
        LIBRARIAN_ALLOWED_EMAIL,
    ):
        return False

    try:
        verified_until = float(
            session.get(
                "librarian_otp_verified_until",
                0,
            )
        )
    except (TypeError, ValueError):
        return False

    return time.time() <= verified_until


def _request_client_ip():
    """Read the first proxy address or the direct remote address."""
    forwarded_for = request.headers.get(
        "X-Forwarded-For",
        "",
    )

    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip()

    return request.remote_addr or ""


def _seconds_since(timestamp_value):
    """Return elapsed seconds for an SQLite local timestamp."""
    parsed_value = _parse_local_datetime(
        timestamp_value
    )

    if parsed_value is None:
        return None

    return max(
        0,
        int(
            (
                datetime.now()
                - parsed_value
            ).total_seconds()
        ),
    )


@app.route(
    "/",
    methods=["GET", "POST"],
)
def login():
    """Main Librarian username and password login."""
    if (
        "username" in session
        and session.get("role") == "librarian"
    ):
        return redirect(
            url_for("dashboard")
        )

    if request.method == "POST":
        username = request.form.get(
            "username",
            "",
        ).strip()
        password = request.form.get(
            "password",
            "",
        )

        if (
            secrets.compare_digest(
                username,
                LIBRARIAN_USERNAME,
            )
            and secrets.compare_digest(
                password,
                LIBRARIAN_PASSWORD,
            )
        ):
            session.clear()
            session["username"] = username
            session["role"] = "librarian"
            session[
                "librarian_login_at"
            ] = time.time()

            flash(
                "Welcome to the Aureon Librarian Dashboard!",
                "success",
            )
            return redirect(
                url_for("dashboard")
            )

        flash(
            "Incorrect Librarian username or password.",
            "danger",
        )

    return render_template(
        "login.html"
    )


@app.route("/logout")
def logout():
    session.clear()
    flash(
        "You have logged out successfully.",
        "success",
    )
    return redirect(url_for("login"))


# -------------------------------------------------
# DASHBOARD
# -------------------------------------------------

@app.route("/dashboard")
@login_required
def dashboard():
    connection = get_db_connection()

    total_books = connection.execute(
        """
        SELECT COALESCE(SUM(total_copies), 0)
        FROM books
        """
    ).fetchone()[0]

    available_books = connection.execute(
        """
        SELECT COALESCE(SUM(available_copies), 0)
        FROM books
        """
    ).fetchone()[0]

    total_members = connection.execute(
        """
        SELECT COUNT(*)
        FROM members
        """
    ).fetchone()[0]

    issued_books = connection.execute(
        """
        SELECT COUNT(*)
        FROM transactions
        WHERE status = 'Issued'
        """
    ).fetchone()[0]

    overdue_count = connection.execute(
        """
        SELECT COUNT(*)
        FROM transactions
        WHERE status = 'Issued'
          AND date(due_date) < date('now', 'localtime')
        """
    ).fetchone()[0]

    recent_transactions = connection.execute(
        """
        SELECT
            transactions.id,
            books.title,
            members.name,
            transactions.issue_date,
            transactions.due_date,
            transactions.status
        FROM transactions
        JOIN books
          ON books.id = transactions.book_id
        JOIN members
          ON members.id = transactions.member_id
        ORDER BY transactions.id DESC
        LIMIT 5
        """
    ).fetchall()

    connection.close()

    return render_template(
        "dashboard.html",
        total_books=total_books,
        available_books=available_books,
        total_members=total_members,
        issued_books=issued_books,
        overdue_count=overdue_count,
        recent_transactions=recent_transactions,
    )

# -------------------------------------------------
# ISBN AUTO-FILL API
# -------------------------------------------------

@app.route("/api/book-by-isbn/<isbn>")
@login_required
def book_by_isbn(isbn):
    """Find a book locally, then through Google Books and Open Library."""
    try:
        cleaned_isbn = clean_isbn(isbn)

        if not is_valid_isbn_format(cleaned_isbn):
            return jsonify({
                "success": False,
                "message": "Please enter a valid ISBN-10 or ISBN-13.",
            }), 400

        connection = get_db_connection()
        try:
            local_book = connection.execute(
                """
                SELECT isbn, title, author, publisher, published_year,
                       category, description, cover_url
                FROM books
                WHERE UPPER(REPLACE(REPLACE(isbn, '-', ''), ' ', '')) = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (cleaned_isbn,),
            ).fetchone()
        finally:
            connection.close()

        if local_book is not None:
            return jsonify({
                "success": True,
                "source": "Aureon Local Catalog",
                "book": {
                    "isbn": local_book["isbn"] or cleaned_isbn,
                    "title": local_book["title"] or "",
                    "author": local_book["author"] or "",
                    "publisher": local_book["publisher"] or "",
                    "published_year": str(local_book["published_year"] or ""),
                    "category": local_book["category"] or "",
                    "description": local_book["description"] or "",
                    "cover_url": local_book["cover_url"] or "",
                },
            })

        headers = {
            "User-Agent": "Aureon-Library-Management-System/2.0 (RSIET Library)",
            "Accept": "application/json",
        }

        try:
            response = requests.get(
                "https://www.googleapis.com/books/v1/volumes",
                params={"q": f"isbn:{cleaned_isbn}", "maxResults": 5, "printType": "books"},
                headers=headers,
                timeout=15,
            )
            response.raise_for_status()
            data = response.json()
            for item in data.get("items", []):
                info = item.get("volumeInfo", {})
                title = str(info.get("title", "")).strip()
                if not title:
                    continue
                authors = info.get("authors", [])
                categories = info.get("categories", [])
                images = info.get("imageLinks", {})
                published_date = str(info.get("publishedDate", ""))
                cover_url = (images.get("extraLarge") or images.get("large") or
                             images.get("medium") or images.get("thumbnail") or
                             images.get("smallThumbnail") or "").replace("http://", "https://")
                return jsonify({
                    "success": True,
                    "source": "Google Books",
                    "book": {
                        "isbn": cleaned_isbn,
                        "title": title,
                        "author": ", ".join(authors),
                        "publisher": info.get("publisher", "") or "",
                        "published_year": published_date[:4] if published_date[:4].isdigit() else "",
                        "category": categories[0] if categories else "",
                        "description": info.get("description", "") or "",
                        "cover_url": cover_url,
                    },
                })
        except (requests.RequestException, ValueError) as error:
            app.logger.warning("Google Books ISBN lookup failed: %s", error)

        try:
            book_key = f"ISBN:{cleaned_isbn}"
            response = requests.get(
                "https://openlibrary.org/api/books",
                params={"bibkeys": book_key, "jscmd": "data", "format": "json"},
                headers=headers,
                timeout=15,
            )
            response.raise_for_status()
            book = response.json().get(book_key)
            if book:
                authors = [item.get("name", "") for item in book.get("authors", []) if item.get("name")]
                publishers = [item.get("name", "") for item in book.get("publishers", []) if item.get("name")]
                subjects = [item.get("name", "") for item in book.get("subjects", []) if item.get("name")]
                cover = book.get("cover", {})
                published = str(book.get("publish_date", ""))
                year = next((part for part in published.replace("-", " ").replace("/", " ").split()
                             if len(part) == 4 and part.isdigit()), "")
                return jsonify({
                    "success": True,
                    "source": "Open Library",
                    "book": {
                        "isbn": cleaned_isbn,
                        "title": book.get("title", "") or "",
                        "author": ", ".join(authors),
                        "publisher": publishers[0] if publishers else "",
                        "published_year": year,
                        "category": subjects[0] if subjects else "",
                        "description": "",
                        "cover_url": cover.get("large") or cover.get("medium") or cover.get("small") or "",
                    },
                })
        except (requests.RequestException, ValueError) as error:
            app.logger.warning("Open Library ISBN lookup failed: %s", error)

        return jsonify({
            "success": False,
            "manual_entry": True,
            "message": (
                "No online details were found for this ISBN. "
                "Enter the book details manually and save it; "
                "Aureon will find it locally next time."
            ),
        }), 404

    except Exception as error:
        app.logger.exception("ISBN lookup crashed for ISBN: %s", isbn)
        return jsonify({
            "success": False,
            "message": f"ISBN lookup failed: {type(error).__name__}: {error}",
        }), 500


# -------------------------------------------------
# BOOKS
# -------------------------------------------------

@app.route("/books", methods=["GET", "POST"])
@login_required
def books():
    connection = get_db_connection()

    if request.method == "POST":
        isbn = clean_isbn(
            request.form.get("isbn", "")
        )
        title = request.form.get(
            "title",
            "",
        ).strip()
        author = request.form.get(
            "author",
            "",
        ).strip()
        publisher = request.form.get(
            "publisher",
            "",
        ).strip()
        published_year = request.form.get(
            "published_year",
            "",
        ).strip()
        category = request.form.get(
            "category",
            "",
        ).strip()
        description = request.form.get(
            "description",
            "",
        ).strip()
        cover_url = request.form.get(
            "cover_url",
            "",
        ).strip()

        try:
            copies = int(
                request.form.get("copies", "1")
            )
        except (TypeError, ValueError):
            copies = 0

        if not title or not author:
            connection.close()
            flash(
                "Title and author are required.",
                "danger",
            )
            return redirect(url_for("books"))

        if copies < 1:
            connection.close()
            flash(
                "Copies must be at least 1.",
                "danger",
            )
            return redirect(url_for("books"))

        if isbn and not is_valid_isbn_format(isbn):
            connection.close()
            flash(
                "Please enter a valid ISBN-10 or ISBN-13.",
                "danger",
            )
            return redirect(url_for("books"))

        if isbn:
            existing_book = connection.execute(
                """
                SELECT id, title
                FROM books
                WHERE UPPER(REPLACE(REPLACE(
                    isbn, '-', ''
                ), ' ', '')) = ?
                LIMIT 1
                """,
                (isbn,),
            ).fetchone()

            if existing_book is not None:
                connection.close()
                flash(
                    (
                        "A book with this ISBN already exists: "
                        f"{existing_book['title']}."
                    ),
                    "warning",
                )
                return redirect(url_for("books"))

        connection.execute(
            """
            INSERT INTO books (
                isbn,
                title,
                author,
                publisher,
                published_year,
                category,
                description,
                cover_url,
                total_copies,
                available_copies
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                isbn or None,
                title,
                author,
                publisher,
                published_year,
                category,
                description,
                cover_url,
                copies,
                copies,
            ),
        )

        connection.commit()
        connection.close()

        flash(
            "Book added successfully.",
            "success",
        )
        return redirect(url_for("books"))

    search = request.args.get(
        "search",
        "",
    ).strip()

    category = request.args.get(
        "category",
        "",
    ).strip()

    query = """
    SELECT
        books.*,
        digital_books.id AS digital_book_id,
        digital_books.file_name AS pdf_filename,
        digital_books.read_price,
        digital_books.download_price,
        digital_books.is_active AS digital_is_active
    FROM books
    LEFT JOIN digital_books
      ON LOWER(TRIM(digital_books.title))
         = LOWER(TRIM(books.title))
     AND LOWER(TRIM(digital_books.author))
         = LOWER(TRIM(books.author))
    WHERE 1 = 1
"""
    values = []

    if search:
        query += """
            AND (
                books.title LIKE ?
                OR books.author LIKE ?
                OR books.isbn LIKE ?
                OR books.publisher LIKE ?
            )
        """
        search_value = f"%{search}%"
        values.extend(
            [
                search_value,
                search_value,
                search_value,
                search_value,
            ]
        )

    if category:
        query += " AND books.category = ?"
        values.append(category)

    query += " ORDER BY books.title"

    books_list = connection.execute(
        query,
        values,
    ).fetchall()

    connection.close()

    return render_template(
        "books.html",
        books=books_list,
    )


@app.route(
    "/books/delete/<int:book_id>",
    methods=["POST"],
)
@login_required
def delete_book(book_id):
    connection = get_db_connection()

    active_issue = connection.execute(
        """
        SELECT id
        FROM transactions
        WHERE book_id = ?
          AND status = 'Issued'
        LIMIT 1
        """,
        (book_id,),
    ).fetchone()

    if active_issue:
        connection.close()
        flash(
            "This book is currently issued. "
            "Return it before deleting.",
            "warning",
        )
        return redirect(url_for("books"))

    connection.execute(
        """
        DELETE FROM books
        WHERE id = ?
        """,
        (book_id,),
    )
    connection.commit()
    connection.close()

    flash(
        "Book deleted successfully.",
        "success",
    )
    return redirect(url_for("books"))


@app.route(
    "/books/edit/<int:book_id>",
    methods=["GET", "POST"],
)
@login_required
def edit_book(book_id):
    connection = get_db_connection()

    book = connection.execute(
        """
        SELECT *
        FROM books
        WHERE id = ?
        """,
        (book_id,),
    ).fetchone()

    if book is None:
        connection.close()
        flash("Book not found.", "danger")
        return redirect(url_for("books"))

    if request.method == "POST":
        isbn = clean_isbn(
            request.form.get(
                "isbn",
                book["isbn"] or "",
            )
        )
        title = request.form.get(
            "title",
            "",
        ).strip()
        author = request.form.get(
            "author",
            "",
        ).strip()
        publisher = request.form.get(
            "publisher",
            book["publisher"] or "",
        ).strip()
        published_year = request.form.get(
            "published_year",
            book["published_year"] or "",
        ).strip()
        category = request.form.get(
            "category",
            "",
        ).strip()
        description = request.form.get(
            "description",
            book["description"] or "",
        ).strip()
        cover_url = request.form.get(
            "cover_url",
            book["cover_url"] or "",
        ).strip()

        try:
            total_copies = int(
                request.form.get("copies", "1")
            )
        except (TypeError, ValueError):
            total_copies = 0

        issued_copies = (
            book["total_copies"]
            - book["available_copies"]
        )

        duplicate_isbn = None

        if isbn:
            duplicate_isbn = connection.execute(
                """
                SELECT id, title
                FROM books
                WHERE UPPER(REPLACE(REPLACE(
                    isbn, '-', ''
                ), ' ', '')) = ?
                  AND id != ?
                LIMIT 1
                """,
                (
                    isbn,
                    book_id,
                ),
            ).fetchone()

        if not title or not author:
            flash(
                "Title and author are required.",
                "danger",
            )
        elif total_copies < 1:
            flash(
                "Copies must be at least 1.",
                "danger",
            )
        elif total_copies < issued_copies:
            flash(
                f"{issued_copies} copies are "
                "currently issued.",
                "warning",
            )
        elif isbn and not is_valid_isbn_format(isbn):
            flash(
                "Please enter a valid ISBN-10 or ISBN-13.",
                "danger",
            )
        elif duplicate_isbn is not None:
            flash(
                (
                    "A book with this ISBN already exists: "
                    f"{duplicate_isbn['title']}."
                ),
                "warning",
            )
        else:
            available_copies = (
                total_copies - issued_copies
            )

            connection.execute(
                """
                UPDATE books
                SET isbn = ?,
                    title = ?,
                    author = ?,
                    publisher = ?,
                    published_year = ?,
                    category = ?,
                    description = ?,
                    cover_url = ?,
                    total_copies = ?,
                    available_copies = ?
                WHERE id = ?
                """,
                (
                    isbn or None,
                    title,
                    author,
                    publisher,
                    published_year,
                    category,
                    description,
                    cover_url,
                    total_copies,
                    available_copies,
                    book_id,
                ),
            )

            connection.commit()
            connection.close()

            flash(
                "Book updated successfully.",
                "success",
            )
            return redirect(url_for("books"))

    connection.close()

    return render_template(
        "edit_book.html",
        book=book,
    )


# -------------------------------------------------
# MEMBERS
# -------------------------------------------------

@app.route(
    "/members",
    methods=["GET", "POST"],
)
@login_required
def members():
    connection = get_db_connection()

    if request.method == "POST":
        name = request.form.get(
            "name",
            "",
        ).strip()
        email = request.form.get(
            "email",
            "",
        ).strip()
        phone = request.form.get(
            "phone",
            "",
        ).strip()
        enrollment_no = request.form.get(
            "enrollment_no",
            "",
        ).strip()
        department = request.form.get(
            "department",
            "",
        ).strip()
        study_year = request.form.get(
            "study_year",
            "",
        ).strip()
        whatsapp_opt_in = (
            1
            if request.form.get("whatsapp_opt_in") == "1"
            else 0
        )

        if not name:
            connection.close()
            flash(
                "Member name is required.",
                "danger",
            )
            return redirect(url_for("members"))

        qr_token = secrets.token_urlsafe(24)

        cursor = connection.execute(
            """
            INSERT INTO members (
                name,
                email,
                phone,
                enrollment_no,
                department,
                study_year,
                whatsapp_opt_in,
                qr_token
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                email,
                phone,
                enrollment_no,
                department,
                study_year,
                whatsapp_opt_in,
                qr_token,
            ),
        )

        member_database_id = cursor.lastrowid
        membership_id = (
            f"AUR-{member_database_id:05d}"
        )

        connection.execute(
            """
            UPDATE members
            SET membership_id = ?
            WHERE id = ?
            """,
            (
                membership_id,
                member_database_id,
            ),
        )

        connection.commit()
        connection.close()

        flash(
            "Member added successfully.",
            "success",
        )
        return redirect(url_for("members"))

    search = request.args.get(
        "search",
        "",
    ).strip()

    if search:
        member_rows = connection.execute(
            """
            SELECT *
            FROM members
            WHERE name LIKE ?
               OR email LIKE ?
               OR phone LIKE ?
               OR enrollment_no LIKE ?
               OR membership_id LIKE ?
               OR department LIKE ?
            ORDER BY id DESC
            """,
            tuple(
                f"%{search}%"
                for _ in range(6)
            ),
        ).fetchall()
    else:
        member_rows = connection.execute(
            """
            SELECT *
            FROM members
            ORDER BY id DESC
            """
        ).fetchall()

    connection.close()

    return render_template(
        "members.html",
        members=member_rows,
        search=search,
    )


@app.route(
    "/members/delete/<int:member_id>",
    methods=["POST"],
)
@login_required
def delete_member(member_id):
    connection = get_db_connection()

    active_issue = connection.execute(
        """
        SELECT id
        FROM transactions
        WHERE member_id = ?
          AND status = 'Issued'
        LIMIT 1
        """,
        (member_id,),
    ).fetchone()

    if active_issue:
        connection.close()
        flash(
            "This member still has an issued book.",
            "warning",
        )
        return redirect(url_for("members"))

    connection.execute(
        """
        DELETE FROM members
        WHERE id = ?
        """,
        (member_id,),
    )
    connection.commit()
    connection.close()

    flash(
        "Member deleted successfully.",
        "success",
    )
    return redirect(url_for("members"))


@app.route(
    "/edit-member/<int:member_id>",
    methods=["GET", "POST"],
)
@login_required
def edit_member(member_id):
    connection = get_db_connection()

    member = connection.execute(
        """
        SELECT *
        FROM members
        WHERE id = ?
        """,
        (member_id,),
    ).fetchone()

    if member is None:
        connection.close()
        flash(
            "Member record was not found.",
            "danger",
        )
        return redirect(url_for("members"))

    if request.method == "POST":
        name = request.form.get(
            "name",
            "",
        ).strip()
        email = request.form.get(
            "email",
            "",
        ).strip()
        phone = request.form.get(
            "phone",
            "",
        ).strip()
        enrollment_no = request.form.get(
            "enrollment_no",
            "",
        ).strip()
        department = request.form.get(
            "department",
            "",
        ).strip()
        study_year = request.form.get(
            "study_year",
            "",
        ).strip()
        whatsapp_opt_in = (
            1
            if request.form.get("whatsapp_opt_in") == "1"
            else 0
        )

        if not name:
            connection.close()
            flash(
                "Member name is required.",
                "danger",
            )
            return redirect(
                url_for(
                    "edit_member",
                    member_id=member_id,
                )
            )

        connection.execute(
            """
            UPDATE members
            SET name = ?,
                email = ?,
                phone = ?,
                enrollment_no = ?,
                department = ?,
                study_year = ?,
                whatsapp_opt_in = ?
            WHERE id = ?
            """,
            (
                name,
                email,
                phone,
                enrollment_no,
                department,
                study_year,
                whatsapp_opt_in,
                member_id,
            ),
        )

        connection.commit()
        connection.close()

        flash(
            "Member updated successfully.",
            "success",
        )
        return redirect(url_for("members"))

    connection.close()

    return render_template(
        "edit_member.html",
        member=member,
    )


# -------------------------------------------------
# ISSUE / RETURN
# -------------------------------------------------

@app.route(
    "/issue-return",
    methods=["GET", "POST"],
)
@login_required
def issue_return():
    connection = get_db_connection()

    if request.method == "POST":
        action = request.form.get(
            "action",
            "",
        ).strip()

        if action == "issue":
            book_id = request.form.get(
                "book_id",
                "",
            ).strip()
            member_id = request.form.get(
                "member_id",
                "",
            ).strip()

            if not book_id or not member_id:
                connection.close()
                flash(
                    "Please select both a book "
                    "and a member.",
                    "warning",
                )
                return redirect(
                    url_for("issue_return")
                )

            book = connection.execute(
                """
                SELECT *
                FROM books
                WHERE id = ?
                """,
                (book_id,),
            ).fetchone()

            member = connection.execute(
                """
                SELECT *
                FROM members
                WHERE id = ?
                """,
                (member_id,),
            ).fetchone()

            if book is None:
                connection.close()
                flash(
                    "Selected book was not found.",
                    "danger",
                )
                return redirect(
                    url_for("issue_return")
                )

            if member is None:
                connection.close()
                flash(
                    "Selected member was not found.",
                    "danger",
                )
                return redirect(
                    url_for("issue_return")
                )

            if book["available_copies"] <= 0:
                connection.close()
                flash(
                    "This book is currently unavailable.",
                    "warning",
                )
                return redirect(
                    url_for("issue_return")
                )

            issued_on = date.today()
            due_on = (
                issued_on
                + timedelta(days=ISSUE_DAYS)
            )

            try:
                connection.execute("BEGIN")

                transaction_cursor = connection.execute(
                    """
                    INSERT INTO transactions (
                        book_id,
                        member_id,
                        issue_date,
                        due_date,
                        status,
                        fine
                    )
                    VALUES (?, ?, ?, ?, 'Issued', 0)
                    """,
                    (
                        book_id,
                        member_id,
                        issued_on.isoformat(),
                        due_on.isoformat(),
                    ),
                )

                result = connection.execute(
                    """
                    UPDATE books
                    SET available_copies =
                        available_copies - 1
                    WHERE id = ?
                      AND available_copies > 0
                    """,
                    (book_id,),
                )

                if result.rowcount != 1:
                    raise psycopg2.IntegrityError(
                        "Book is unavailable."
                    )

                connection.commit()
                transaction_id = transaction_cursor.lastrowid
                _send_transaction_whatsapp(
                    connection,
                    transaction_id,
                    "issued",
                )

            except psycopg2.Error:
                connection.rollback()
                connection.close()

                flash(
                    "The book could not be issued. "
                    "Please try again.",
                    "danger",
                )
                return redirect(
                    url_for("issue_return")
                )

            connection.close()

            flash(
                "Book issued successfully. "
                f"Due date: "
                f"{due_on.strftime('%d-%m-%Y')}.",
                "success",
            )
            return redirect(url_for("issue_return"))

        if action == "return":
            transaction_id = request.form.get(
                "transaction_id",
                "",
            ).strip()

            if not transaction_id:
                connection.close()
                flash(
                    "Please select a book to return.",
                    "warning",
                )
                return redirect(
                    url_for("issue_return")
                )

            transaction = connection.execute(
                """
                SELECT *
                FROM transactions
                WHERE id = ?
                  AND status = 'Issued'
                """,
                (transaction_id,),
            ).fetchone()

            if transaction is None:
                connection.close()
                flash(
                    "Active issue record was not found "
                    "or was already returned.",
                    "danger",
                )
                return redirect(
                    url_for("issue_return")
                )

            returned_on = date.today()
            due_on = date.fromisoformat(
                transaction["due_date"]
            )

            fine = (
                FINE_AMOUNT
                if returned_on > due_on
                else 0
            )

            try:
                connection.execute("BEGIN")

                connection.execute(
                    """
                    UPDATE transactions
                    SET status = 'Returned',
                        return_date = ?,
                        fine = ?
                    WHERE id = ?
                    """,
                    (
                        returned_on.isoformat(),
                        fine,
                        transaction_id,
                    ),
                )

                connection.execute(
                    """
                    UPDATE books
                    SET available_copies =
                        available_copies + 1
                    WHERE id = ?
                    """,
                    (transaction["book_id"],),
                )

                connection.commit()
                _send_transaction_whatsapp(
                    connection,
                    int(transaction_id),
                    "returned",
                )

            except psycopg2.Error:
                connection.rollback()
                connection.close()

                flash(
                    "The book could not be returned. "
                    "Please try again.",
                    "danger",
                )
                return redirect(
                    url_for("issue_return")
                )

            connection.close()

            if fine > 0:
                flash(
                    f"Book returned late. "
                    f"Fine: ₹{fine}.",
                    "warning",
                )
            else:
                flash(
                    "Book returned successfully. "
                    "Fine: ₹0.",
                    "success",
                )

            return redirect(url_for("issue_return"))

        connection.close()
        flash("Invalid action.", "danger")
        return redirect(url_for("issue_return"))

    available_books = connection.execute(
        """
        SELECT *
        FROM books
        WHERE available_copies > 0
        ORDER BY title
        """
    ).fetchall()

    member_rows = connection.execute(
        """
        SELECT *
        FROM members
        ORDER BY name
        """
    ).fetchall()

    active_transactions = connection.execute(
        """
        SELECT
            transactions.id,
            transactions.book_id,
            transactions.member_id,
            transactions.issue_date,
            transactions.due_date,
            books.title,
            members.name
        FROM transactions
        JOIN books
          ON books.id = transactions.book_id
        JOIN members
          ON members.id = transactions.member_id
        WHERE transactions.status = 'Issued'
        ORDER BY transactions.due_date
        """
    ).fetchall()

    history = connection.execute(
        """
        SELECT
            transactions.id,
            books.title,
            books.author,
            members.name,
            transactions.issue_date,
            transactions.due_date,
            transactions.return_date,
            transactions.fine,
            transactions.status
        FROM transactions
        JOIN books
          ON books.id = transactions.book_id
        JOIN members
          ON members.id = transactions.member_id
        ORDER BY transactions.id DESC
        """
    ).fetchall()

    connection.close()

    return render_template(
        "issue_return.html",
        books=available_books,
        members=member_rows,
        active_transactions=active_transactions,
        history=history,
    )


# -------------------------------------------------
# OVERDUE
# -------------------------------------------------

@app.route("/overdue")
@login_required
def overdue():
    connection = get_db_connection()

    overdue_books = connection.execute(
        """
        SELECT
            transactions.id AS transaction_id,
            books.title,
            books.author,
            members.id AS member_id,
            members.name,
            members.membership_id,
            members.phone,
            transactions.issue_date,
            transactions.due_date,

            -- PostgreSQL: calculate number of overdue days
            (CURRENT_DATE - transactions.due_date::date) AS overdue_days,

            ? AS current_fine

        FROM transactions
        JOIN books
            ON books.id = transactions.book_id
        JOIN members
            ON members.id = transactions.member_id

        WHERE transactions.status = 'Issued'
          AND transactions.due_date::date < CURRENT_DATE

        ORDER BY transactions.due_date
        """,
        (FINE_AMOUNT,),
    ).fetchall()

    pending_cash_requests = connection.execute(
        """
        SELECT
            fine_payment_requests.id AS request_id,
            fine_payment_requests.transaction_id,
            fine_payment_requests.member_id,
            fine_payment_requests.amount,
            fine_payment_requests.payment_status,
            fine_payment_requests.cash_requested_at,
            members.name AS member_name,
            members.membership_id,
            members.phone,
            books.title,
            books.author,
            transactions.due_date,
            transactions.return_date,

            -- PostgreSQL: calculate overdue days
            (
                transactions.return_date::date
                - transactions.due_date::date
            ) AS overdue_days

        FROM fine_payment_requests
        JOIN transactions
            ON transactions.id = fine_payment_requests.transaction_id
        JOIN members
            ON members.id = fine_payment_requests.member_id
        JOIN books
            ON books.id = transactions.book_id

        WHERE fine_payment_requests.payment_method = 'Cash'
          AND fine_payment_requests.payment_status = 'Awaiting Cash Confirmation'

        ORDER BY fine_payment_requests.id DESC
        """
    ).fetchall()

    connection.close()

    return render_template(
        "overdue.html",
        overdue_books=overdue_books,
        pending_cash_requests=pending_cash_requests,
    )

# -------------------------------------------------
# MEMBER HISTORY / PROFILE
# -------------------------------------------------

@app.route("/member-history")
@login_required
def member_history():
    connection = get_db_connection()

    search = request.args.get(
        "search",
        "",
    ).strip()

    study_year = request.args.get(
        "study_year",
        "",
    ).strip()

    department = request.args.get(
        "department",
        "",
    ).strip()

    query = """
        SELECT
            members.id,
            members.membership_id,
            members.name,
            members.email,
            members.phone,
            members.enrollment_no,
            members.department,
            members.study_year,

            (
                SELECT COUNT(*)
                FROM transactions
                WHERE transactions.member_id =
                      members.id
            ) AS total_borrowed,

            (
                SELECT COUNT(*)
                FROM transactions
                WHERE transactions.member_id =
                      members.id
                  AND transactions.status = 'Issued'
            ) AS active_books,

            COALESCE(
                (
                    SELECT SUM(transactions.fine)
                    FROM transactions
                    WHERE transactions.member_id =
                          members.id
                ),
                0
            ) AS total_fine

        FROM members
        WHERE 1 = 1
    """

    values = []

    if search:
        search_value = f"%{search}%"

        query += """
            AND (
                members.name LIKE ?
                OR members.enrollment_no LIKE ?
                OR members.membership_id LIKE ?
                OR members.email LIKE ?
                OR members.phone LIKE ?
            )
        """

        values.extend([
            search_value,
            search_value,
            search_value,
            search_value,
            search_value,
        ])

    if study_year:
        query += """
            AND members.study_year = ?
        """
        values.append(study_year)

    if department:
        query += """
            AND members.department LIKE ?
        """
        values.append(f"%{department}%")

    query += """
        ORDER BY members.name
    """

    members_history = connection.execute(
        query,
        values,
    ).fetchall()

    connection.close()

    return render_template(
        "member_history.html",
        members_history=members_history,
    )


def _get_member_summary(
    connection,
    member_id,
):
    return connection.execute(
        """
        SELECT
            COALESCE(
                SUM(
                    CASE
                        WHEN transactions.status = 'Issued'
                        THEN 1
                        ELSE 0
                    END
                ),
                0
            ) AS active_books,

            COALESCE(
                SUM(transactions.fine),
                0
            ) AS total_fine,

            COALESCE(
                SUM(
                    CASE
                        WHEN transactions.fine > 0
                         AND fine_payments.id IS NULL
                        THEN transactions.fine
                        ELSE 0
                    END
                ),
                0
            ) AS unpaid_fine,

            COALESCE(
                SUM(
                    CASE
                        WHEN fine_payments.id IS NOT NULL
                        THEN transactions.fine
                        ELSE 0
                    END
                ),
                0
            ) AS paid_fine
        FROM transactions
        LEFT JOIN fine_payments
          ON fine_payments.transaction_id =
             transactions.id
        WHERE transactions.member_id = ?
        """,
        (member_id,),
    ).fetchone()


def _get_member_history(
    connection,
    member_id,
):
    return connection.execute(
        """
        SELECT
            transactions.id AS transaction_id,
            books.title,
            books.author,
            transactions.issue_date,
            transactions.due_date,
            transactions.return_date,
            transactions.status,
            transactions.fine
        FROM transactions
        JOIN books
          ON books.id = transactions.book_id
        WHERE transactions.member_id = ?
        ORDER BY transactions.id DESC
        """,
        (member_id,),
    ).fetchall()


def _get_fine_records(
    connection,
    member_id,
):
    return connection.execute(
        """
        SELECT
            transactions.id AS transaction_id,
            transactions.fine,
            transactions.return_date,
            books.title,
            fine_payments.id AS payment_id,
            fine_payments.receipt_no,
            fine_payments.payment_method,
            fine_payments.payment_reference,
            fine_payments.paid_at
        FROM transactions
        JOIN books
          ON books.id = transactions.book_id
        LEFT JOIN fine_payments
          ON fine_payments.transaction_id =
             transactions.id
        WHERE transactions.member_id = ?
          AND transactions.fine > 0
        ORDER BY transactions.id DESC
        """,
        (member_id,),
    ).fetchall()


def get_member_by_qr_token(
    connection,
    qr_token,
):
    """Find the member connected to a secure QR token."""
    return connection.execute(
        """
        SELECT *
        FROM members
        WHERE qr_token = ?
        """,
        (qr_token,),
    ).fetchone()


def get_incident_records(
    connection,
    member_id,
):
    """Return lost/damaged reports and payment details."""
    return connection.execute(
        """
        SELECT
            book_incidents.id AS incident_id,
            book_incidents.transaction_id,
            book_incidents.member_id,
            book_incidents.book_id,
            book_incidents.incident_type,
            book_incidents.description,
            book_incidents.noticed_date,
            book_incidents.charge,
            book_incidents.status,
            book_incidents.reported_at,
            book_incidents.reviewed_at,
            book_incidents.reviewed_by,
            book_incidents.admin_note,
            books.title,
            books.author,
            incident_payments.id AS payment_id,
            incident_payments.receipt_no,
            incident_payments.amount,
            incident_payments.payment_method,
            incident_payments.payment_status,
            incident_payments.payment_reference,
            incident_payments.gateway_order_id,
            incident_payments.gateway_payment_id,
            incident_payments.cash_requested_at,
            incident_payments.cash_confirmed_by,
            incident_payments.paid_at
        FROM book_incidents
        JOIN books
          ON books.id = book_incidents.book_id
        LEFT JOIN incident_payments
          ON incident_payments.incident_id =
             book_incidents.id
        WHERE book_incidents.member_id = ?
        ORDER BY book_incidents.id DESC
        """,
        (member_id,),
    ).fetchall()


@app.route("/member/<int:member_id>")
@login_required
def member_profile(member_id):
    connection = get_db_connection()

    member = connection.execute(
        """
        SELECT *
        FROM members
        WHERE id = ?
        """,
        (member_id,),
    ).fetchone()

    if member is None:
        connection.close()
        flash(
            "Member record was not found.",
            "danger",
        )
        return redirect(url_for("members"))

    history = _get_member_history(
        connection,
        member_id,
    )
    member_summary = _get_member_summary(
        connection,
        member_id,
    )
    fine_records = _get_fine_records(
        connection,
        member_id,
    )

    connection.close()

    return render_template(
        "member_profile.html",
        member=member,
        history=history,
        member_summary=member_summary,
        fine_records=fine_records,
    )


# -------------------------------------------------
# QR IMAGE AND PUBLIC SCANNED PAGE
# -------------------------------------------------

@app.route("/member/<int:member_id>/qr")
def member_qr(member_id):
    connection = get_db_connection()

    member = connection.execute(
        """
        SELECT
            id,
            membership_id,
            qr_token
        FROM members
        WHERE id = ?
        """,
        (member_id,),
    ).fetchone()

    if member is None:
        connection.close()
        return "Member was not found.", 404

    qr_token = member["qr_token"]

    if not qr_token:
        qr_token = secrets.token_urlsafe(24)

        connection.execute(
            """
            UPDATE members
            SET qr_token = ?
            WHERE id = ?
            """,
            (
                qr_token,
                member_id,
            ),
        )
        connection.commit()

    connection.close()

    history_url = url_for(
        "member_scan",
        qr_token=qr_token,
        _external=True,
    )

    qr_code = qrcode.QRCode(
        version=1,
        error_correction=(
            qrcode.constants.ERROR_CORRECT_M
        ),
        box_size=8,
        border=4,
    )
    qr_code.add_data(history_url)
    qr_code.make(fit=True)

    qr_image = qr_code.make_image(
        fill_color="black",
        back_color="white",
    )

    image_buffer = BytesIO()
    qr_image.save(
        image_buffer,
        format="PNG",
    )
    image_buffer.seek(0)

    return send_file(
        image_buffer,
        mimetype="image/png",
        max_age=0,
    )


@app.route(
    "/member/<int:member_id>/qr-history"
)
def member_qr_history(member_id):
    """
    Keep old QR links working, then move them to
    the secure token-based scanned page.
    """
    connection = get_db_connection()

    member = connection.execute(
        """
        SELECT qr_token
        FROM members
        WHERE id = ?
        """,
        (member_id,),
    ).fetchone()

    connection.close()

    if member is None:
        return "Member was not found.", 404

    return redirect(
        url_for(
            "member_scan",
            qr_token=member["qr_token"],
        )
    )


@app.route("/scan/<qr_token>")
def member_scan(qr_token):
    """Open the password setup/login page after a membership QR is scanned."""
    connection = get_db_connection()
    member = get_member_by_qr_token(
        connection,
        qr_token,
    )
    connection.close()

    if member is None:
        return "Invalid or expired QR code.", 404

    if not member["card_portal_enabled"]:
        return (
            "This Digital Library Card has been disabled. "
            "Please contact the Librarian.",
            403,
        )

    if _member_session_is_valid(member, qr_token):
        session["card_last_activity"] = time.time()
        return redirect(url_for("member_portal"))

    if (
        not member["card_password_hash"]
        or member["card_reset_required"]
    ):
        return redirect(
            url_for(
                "member_card_setup",
                qr_token=qr_token,
            )
        )

    return redirect(
        url_for(
            "member_card_login",
            qr_token=qr_token,
        )
    )


@app.route(
    "/scan/<qr_token>/report-incident/"
    "<int:transaction_id>",
    methods=["POST"],
)
def report_book_incident(
    qr_token,
    transaction_id,
):
    connection = get_db_connection()

    member = get_member_by_qr_token(
        connection,
        qr_token,
    )

    if member is None:
        connection.close()
        return "Invalid QR code.", 404

    incident_type = request.form.get(
        "incident_type",
        "",
    ).strip()

    description = request.form.get(
        "description",
        "",
    ).strip()

    noticed_date = request.form.get(
        "noticed_date",
        "",
    ).strip()

    if incident_type not in {
        "Lost",
        "Damaged",
    }:
        connection.close()
        flash(
            "Please select Lost or Damaged.",
            "danger",
        )
        return redirect(
            url_for(
                "member_scan",
                qr_token=qr_token,
            )
        )

    if not description:
        connection.close()
        flash(
            "Please explain what happened to the book.",
            "danger",
        )
        return redirect(
            url_for(
                "member_scan",
                qr_token=qr_token,
            )
        )

    transaction = connection.execute(
        """
        SELECT
            id,
            member_id,
            book_id,
            status
        FROM transactions
        WHERE id = ?
          AND member_id = ?
        """,
        (
            transaction_id,
            member["id"],
        ),
    ).fetchone()

    if (
        transaction is None
        or transaction["status"] != "Issued"
    ):
        connection.close()
        flash(
            "This book is not currently issued "
            "to this member.",
            "danger",
        )
        return redirect(
            url_for(
                "member_scan",
                qr_token=qr_token,
            )
        )

    existing_incident = connection.execute(
        """
        SELECT id
        FROM book_incidents
        WHERE transaction_id = ?
          AND status IN (
              'Pending',
              'Approved',
              'Paid',
              'Resolved'
          )
        LIMIT 1
        """,
        (transaction_id,),
    ).fetchone()

    if existing_incident is not None:
        connection.close()
        flash(
            "A Lost or Damaged report already "
            "exists for this issued book.",
            "warning",
        )
        return redirect(
            url_for(
                "member_scan",
                qr_token=qr_token,
            )
        )

    try:
        connection.execute(
            """
            INSERT INTO book_incidents (
                transaction_id,
                member_id,
                book_id,
                incident_type,
                description,
                noticed_date,
                charge,
                status,
                reported_at
            )
            VALUES (
                ?, ?, ?, ?, ?, ?,
                0,
                'Pending',
                datetime('now', 'localtime')
            )
            """,
            (
                transaction_id,
                member["id"],
                transaction["book_id"],
                incident_type,
                description,
                noticed_date or None,
            ),
        )

        connection.commit()

    except psycopg2.Error:
        connection.rollback()
        connection.close()
        flash(
            "The report could not be submitted.",
            "danger",
        )
        return redirect(
            url_for(
                "member_scan",
                qr_token=qr_token,
            )
        )

    connection.close()

    flash(
        f"{incident_type} book report submitted. "
        "Waiting for librarian approval.",
        "success",
    )

    return redirect(
        url_for(
            "member_scan",
            qr_token=qr_token,
        )
    )


# -------------------------------------------------
# LOST / DAMAGED - LIBRARIAN CONTROL
# -------------------------------------------------

@app.route("/librarian/incidents")
@login_required
def incidents():
    connection = get_db_connection()

    incident_rows = connection.execute(
        """
        SELECT
            book_incidents.id AS incident_id,
            book_incidents.transaction_id,
            book_incidents.member_id,
            book_incidents.book_id,
            book_incidents.incident_type,
            book_incidents.description,
            book_incidents.noticed_date,
            book_incidents.charge,
            book_incidents.status AS status,
            book_incidents.reported_at,
            book_incidents.reviewed_at,
            book_incidents.reviewed_by,
            book_incidents.admin_note,

            members.name AS member_name,
            members.enrollment_no,
            members.membership_id,
            members.department,
            members.study_year,

            books.title,
            books.author,

            transactions.issue_date,
            transactions.due_date,
            transactions.return_date,
            transactions.status AS transaction_status,

            incident_payments.id AS payment_id,
            incident_payments.receipt_no,
            incident_payments.amount,
            incident_payments.payment_method,
            incident_payments.payment_status,
            incident_payments.payment_reference,
            incident_payments.cash_requested_at,
            incident_payments.cash_confirmed_by,
            incident_payments.paid_at

        FROM book_incidents

        JOIN members
          ON members.id = book_incidents.member_id

        JOIN books
          ON books.id = book_incidents.book_id

        JOIN transactions
          ON transactions.id =
             book_incidents.transaction_id

        LEFT JOIN incident_payments
          ON incident_payments.incident_id =
             book_incidents.id

        ORDER BY
            CASE book_incidents.status
                WHEN 'Pending' THEN 1
                WHEN 'Approved' THEN 2
                WHEN 'Paid' THEN 3
                WHEN 'Rejected' THEN 4
                ELSE 5
            END,
            book_incidents.id DESC
        """
    ).fetchall()

    connection.close()

    return render_template(
        "incidents.html",
        incidents=incident_rows,
    )


@app.route(
    "/librarian/incidents/<int:incident_id>/review",
    methods=["POST"],
)
@login_required
def review_incident(incident_id):
    connection = get_db_connection()

    incident = connection.execute(
        """
        SELECT
            book_incidents.*,
            transactions.status AS transaction_status
        FROM book_incidents
        JOIN transactions
          ON transactions.id =
             book_incidents.transaction_id
        WHERE book_incidents.id = ?
        """,
        (incident_id,),
    ).fetchone()

    if incident is None:
        connection.close()
        flash(
            "Lost or damaged report was not found.",
            "danger",
        )
        return redirect(url_for("incidents"))

    if incident["status"] != "Pending":
        connection.close()
        flash(
            "This report has already been reviewed.",
            "warning",
        )
        return redirect(url_for("incidents"))

    action = request.form.get(
        "action",
        "",
    ).strip().lower()

    librarian_note = request.form.get(
        "admin_note",
        "",
    ).strip()

    reviewed_by = session.get(
        "username",
        "Librarian",
    )

    if action == "reject":
        try:
            connection.execute(
                """
                UPDATE book_incidents
                SET status = 'Rejected',
                    charge = 0,
                    reviewed_at =
                        datetime('now', 'localtime'),
                    reviewed_by = ?,
                    admin_note = ?
                WHERE id = ?
                  AND status = 'Pending'
                """,
                (
                    reviewed_by,
                    librarian_note or None,
                    incident_id,
                ),
            )

            connection.execute(
                """
                DELETE FROM incident_payments
                WHERE incident_id = ?
                  AND payment_status != 'Paid'
                """,
                (incident_id,),
            )

            connection.commit()
            _send_incident_whatsapp(
                connection,
                incident_id,
                "Rejected",
            )

        except psycopg2.Error:
            connection.rollback()
            connection.close()
            flash(
                "The report could not be rejected.",
                "danger",
            )
            return redirect(url_for("incidents"))

        connection.close()
        flash(
            "The report was rejected by the Librarian.",
            "success",
        )
        return redirect(url_for("incidents"))

    if action != "approve":
        connection.close()
        flash("Invalid review action.", "danger")
        return redirect(url_for("incidents"))

    charge_text = request.form.get(
        "charge",
        "",
    ).strip()

    try:
        charge = int(charge_text)
    except (TypeError, ValueError):
        connection.close()
        flash(
            "Enter a valid charge amount.",
            "danger",
        )
        return redirect(url_for("incidents"))

    if charge < 0:
        connection.close()
        flash(
            "Charge cannot be negative.",
            "danger",
        )
        return redirect(url_for("incidents"))

    if incident["transaction_status"] != "Issued":
        connection.close()
        flash(
            "This book is no longer marked as Issued, "
            "so the report cannot be approved.",
            "warning",
        )
        return redirect(url_for("incidents"))

    transaction_status = (
        "Lost"
        if incident["incident_type"] == "Lost"
        else "Damaged"
    )

    try:
        connection.execute("BEGIN")

        result = connection.execute(
            """
            UPDATE book_incidents
            SET status = 'Approved',
                charge = ?,
                reviewed_at =
                    datetime('now', 'localtime'),
                reviewed_by = ?,
                admin_note = ?
            WHERE id = ?
              AND status = 'Pending'
            """,
            (
                charge,
                reviewed_by,
                librarian_note or None,
                incident_id,
            ),
        )

        if result.rowcount != 1:
            raise psycopg2.IntegrityError(
                "The report was already reviewed."
            )

        connection.execute(
            """
            UPDATE transactions
            SET status = ?,
                return_date = COALESCE(
                    return_date,
                    date('now', 'localtime')
                )
            WHERE id = ?
              AND status = 'Issued'
            """,
            (
                transaction_status,
                incident["transaction_id"],
            ),
        )

        connection.commit()
        _send_incident_whatsapp(
            connection,
            incident_id,
            "Approved",
        )

    except psycopg2.Error:
        connection.rollback()
        connection.close()
        flash(
            "The report could not be approved.",
            "danger",
        )
        return redirect(url_for("incidents"))

    connection.close()

    flash(
        f"Report approved. Charge added: ₹{charge}.",
        "success",
    )
    return redirect(url_for("incidents"))


# -------------------------------------------------
# INCIDENT CASH PAYMENT
# -------------------------------------------------

@app.route(
    "/scan/<qr_token>/incident/"
    "<int:incident_id>/cash-request",
    methods=["POST"],
)
@app.route(
    "/member-portal/incident/"
    "<int:incident_id>/cash-request",
    methods=["POST"],
)
def request_incident_cash(
    incident_id,
    qr_token=None,
):
    """Let the logged-in student request Cash payment for an incident."""
    if qr_token:
        connection = get_db_connection()
        member = get_member_by_qr_token(
            connection,
            qr_token,
        )
        connection.close()

        if (
            member is None
            or not _member_session_is_valid(member, qr_token)
        ):
            flash(
                "Please log in before requesting Cash payment.",
                "warning",
            )
            return redirect(
                url_for(
                    "member_card_login",
                    qr_token=qr_token,
                )
            )
    else:
        member_id = session.get("member_id")
        connection = get_db_connection()
        member = connection.execute(
            """
            SELECT *
            FROM members
            WHERE id = ?
            """,
            (member_id,),
        ).fetchone()
        connection.close()

        if (
            member is None
            or not _member_session_is_valid(
                member,
                session.get("member_qr_token"),
            )
        ):
            flash(
                "Please log in before requesting Cash payment.",
                "warning",
            )
            return redirect(url_for("login"))

        qr_token = member["qr_token"]

    connection = get_db_connection()

    incident = connection.execute(
        """
        SELECT *
        FROM book_incidents
        WHERE id = ?
          AND member_id = ?
          AND status = 'Approved'
          AND charge > 0
        """,
        (
            incident_id,
            member["id"],
        ),
    ).fetchone()

    if incident is None:
        connection.close()
        flash(
            "Cash payment is not available for this report.",
            "danger",
        )
        return redirect(
            url_for("member_portal") + "#payments"
        )

    existing_payment = connection.execute(
        """
        SELECT *
        FROM incident_payments
        WHERE incident_id = ?
        """,
        (incident_id,),
    ).fetchone()

    if (
        existing_payment is not None
        and existing_payment["payment_status"] == "Paid"
    ):
        connection.close()
        flash(
            "This charge has already been paid.",
            "warning",
        )
        return redirect(
            url_for("member_portal") + "#payments"
        )

    try:
        if existing_payment is None:
            connection.execute(
                """
                INSERT INTO incident_payments (
                    incident_id,
                    amount,
                    payment_method,
                    payment_status,
                    cash_requested_at
                )
                VALUES (
                    ?, ?, 'Cash',
                    'Awaiting Cash Confirmation',
                    datetime('now', 'localtime')
                )
                """,
                (
                    incident_id,
                    incident["charge"],
                ),
            )
        else:
            connection.execute(
                """
                UPDATE incident_payments
                SET amount = ?,
                    payment_method = 'Cash',
                    payment_status =
                        'Awaiting Cash Confirmation',
                    payment_reference = NULL,
                    gateway_order_id = NULL,
                    gateway_payment_id = NULL,
                    gateway_signature = NULL,
                    cash_requested_at =
                        datetime('now', 'localtime'),
                    cash_confirmed_by = NULL,
                    paid_at = NULL,
                    receipt_no = NULL
                WHERE incident_id = ?
                """,
                (
                    incident["charge"],
                    incident_id,
                ),
            )

        _create_member_notification(
            connection,
            member["id"],
            "Payment",
            "Cash payment request submitted",
            (
                f"Your Cash payment request for the "
                f"{incident['incident_type'].lower()} book charge "
                f"of ₹{incident['charge']} was submitted. "
                "Please pay the Librarian."
            ),
            priority="Important",
            unique_key=(
                f"incident:{incident_id}:cash-request:"
                f"{date.today().isoformat()}"
            ),
        )

        connection.commit()

    except psycopg2.Error:
        connection.rollback()
        connection.close()
        flash(
            "Cash payment request could not be saved.",
            "danger",
        )
        return redirect(
            url_for("member_portal") + "#payments"
        )

    connection.close()

    flash(
        "Cash payment request submitted. "
        "Pay the amount to the Librarian.",
        "success",
    )

    return redirect(
        url_for("member_portal") + "#payments"
    )


@app.route(
    "/librarian/incident-payment/"
    "<int:payment_id>/confirm-cash",
    methods=["POST"],
)
@login_required
def confirm_incident_cash(payment_id):
    connection = get_db_connection()

    payment = connection.execute(
        """
        SELECT
            incident_payments.*,
            book_incidents.id AS incident_record_id,
            book_incidents.member_id
        FROM incident_payments
        JOIN book_incidents
          ON book_incidents.id =
             incident_payments.incident_id
        WHERE incident_payments.id = ?
          AND incident_payments.payment_method = 'Cash'
          AND incident_payments.payment_status =
              'Awaiting Cash Confirmation'
        """,
        (payment_id,),
    ).fetchone()

    if payment is None:
        connection.close()
        flash(
            "Cash payment request was not found.",
            "danger",
        )
        return redirect(url_for("incidents"))

    receipt_no = f"AUR-I-{payment_id:05d}"
    confirmed_by = session.get(
        "username",
        "Librarian",
    )

    try:
        connection.execute(
            """
            UPDATE incident_payments
            SET receipt_no = ?,
                payment_status = 'Paid',
                payment_reference = ?,
                cash_confirmed_by = ?,
                paid_at =
                    datetime('now', 'localtime')
            WHERE id = ?
            """,
            (
                receipt_no,
                f"Cash received by {confirmed_by}",
                confirmed_by,
                payment_id,
            ),
        )

        connection.execute(
            """
            UPDATE book_incidents
            SET status = 'Paid'
            WHERE id = ?
            """,
            (payment["incident_record_id"],),
        )

        connection.commit()
        _send_member_payment_whatsapp(
            connection,
            payment["member_id"],
            payment["amount"],
            "Lost or damaged book charge",
            f"incident-payment:{payment_id}:paid",
        )
        _send_incident_whatsapp(
            connection,
            payment["incident_record_id"],
            "Paid",
        )

    except psycopg2.Error:
        connection.rollback()
        connection.close()
        flash(
            "Cash payment could not be confirmed.",
            "danger",
        )
        return redirect(url_for("incidents"))

    connection.close()

    flash(
        "Cash payment confirmed. "
        "The receipt is now available.",
        "success",
    )

    return redirect(url_for("incidents"))


# -------------------------------------------------
# RAZORPAY ONLINE / UPI 
# -------------------------------------------------

@app.route(
    "/scan/<qr_token>/incident/"
    "<int:incident_id>/create-order",
    methods=["POST"],
)
def create_incident_order(qr_token, incident_id):
    connection = get_db_connection()
    member = get_member_by_qr_token(connection, qr_token)

    if member is None:
        connection.close()
        return jsonify({"success": False, "message": "Invalid QR code."}), 404

    incident = connection.execute(
        """
        SELECT book_incidents.*, books.title
        FROM book_incidents
        JOIN books ON books.id = book_incidents.book_id
        WHERE book_incidents.id = ?
          AND book_incidents.member_id = ?
          AND book_incidents.status = 'Approved'
          AND book_incidents.charge > 0
        """,
        (incident_id, member["id"]),
    ).fetchone()

    if incident is None:
        connection.close()
        return jsonify({"success": False, "message": "Payment is unavailable."}), 400

    existing_payment = connection.execute(
        "SELECT * FROM incident_payments WHERE incident_id = ?",
        (incident_id,),
    ).fetchone()

    if existing_payment is not None and existing_payment["payment_status"] == "Paid":
        connection.close()
        return jsonify({"success": False, "message": "This charge is already paid."}), 400

    client = get_razorpay_client()
    if client is None:
        connection.close()
        return jsonify({
            "success": False,
            "message": "Razorpay Test keys are not configured.",
        }), 503

    requested_method = request.form.get("requested_method", "Online").strip()
    if requested_method != "Online":
        connection.close()
        return jsonify({
            "success": False,
            "message": "Only Online / UPI payment is available.",
        }), 400

    amount_in_paise = int(incident["charge"]) * 100

    try:
        order = client.order.create({
            "amount": amount_in_paise,
            "currency": "INR",
            "receipt": f"incident_{incident_id}_{secrets.token_hex(4)}",
            "notes": {
                "incident_id": str(incident_id),
                "member_id": str(member["id"]),
                "requested_method": requested_method,
            },
        })
    except Exception as error:
        connection.close()
        app.logger.exception("Razorpay incident order failed")
        return jsonify({
            "success": False,
            "message": f"Razorpay order error: {error}",
        }), 503

    try:
        if existing_payment is None:
            cursor = connection.execute(
                """
                INSERT INTO incident_payments (
                    incident_id, amount, payment_method, payment_status, gateway_order_id
                ) VALUES (?, ?, ?, 'Pending', ?)
                """,
                (incident_id, incident["charge"], requested_method, order["id"]),
            )
            payment_record_id = cursor.lastrowid
        else:
            payment_record_id = existing_payment["id"]
            connection.execute(
                """
                UPDATE incident_payments
                SET amount = ?, payment_method = ?, payment_status = 'Pending',
                    payment_reference = NULL, gateway_order_id = ?,
                    gateway_payment_id = NULL, gateway_signature = NULL,
                    cash_requested_at = NULL, cash_confirmed_by = NULL,
                    paid_at = NULL, receipt_no = NULL
                WHERE id = ?
                """,
                (incident["charge"], requested_method, order["id"], payment_record_id),
            )
        connection.commit()
    except psycopg2.Error:
        connection.rollback()
        connection.close()
        return jsonify({
            "success": False,
            "message": "Payment record could not be saved.",
        }), 500

    connection.close()
    return jsonify({
        "success": True,
        "key_id": RAZORPAY_KEY_ID,
        "order_id": order["id"],
        "amount": amount_in_paise,
        "currency": "INR",
        "payment_record_id": payment_record_id,
        "member_name": member["name"],
        "member_email": member["email"] or "",
        "member_phone": member["phone"] or "",
        "membership_id": member["membership_id"] or "",
        "book_title": incident["title"],
        "incident_type": incident["incident_type"],
    })


@app.route(
    "/scan/<qr_token>/incident/"
    "<int:incident_id>/verify-payment",
    methods=["POST"],
)
def verify_incident_payment(
    qr_token,
    incident_id,
):
    connection = get_db_connection()

    member = get_member_by_qr_token(
        connection,
        qr_token,
    )

    if member is None:
        connection.close()
        return jsonify({
            "success": False,
            "message": "Invalid QR code.",
        }), 404

    data = request.get_json(
        silent=True,
    ) or {}

    razorpay_order_id = str(
        data.get(
            "razorpay_order_id",
            "",
        )
    ).strip()

    razorpay_payment_id = str(
        data.get(
            "razorpay_payment_id",
            "",
        )
    ).strip()

    razorpay_signature = str(
        data.get(
            "razorpay_signature",
            "",
        )
    ).strip()

    if not all((
        razorpay_order_id,
        razorpay_payment_id,
        razorpay_signature,
    )):
        connection.close()
        return jsonify({
            "success": False,
            "message": (
                "Incomplete payment information."
            ),
        }), 400

    payment = connection.execute(
        """
        SELECT
            incident_payments.*,
            book_incidents.member_id,
            book_incidents.charge
        FROM incident_payments
        JOIN book_incidents
          ON book_incidents.id =
             incident_payments.incident_id
        WHERE incident_payments.incident_id = ?
          AND book_incidents.member_id = ?
          AND incident_payments.gateway_order_id = ?
        """,
        (
            incident_id,
            member["id"],
            razorpay_order_id,
        ),
    ).fetchone()

    if payment is None:
        connection.close()
        return jsonify({
            "success": False,
            "message": (
                "Payment record was not found."
            ),
        }), 404

    if payment["payment_status"] == "Paid":
        connection.close()
        return jsonify({
            "success": True,
            "payment_id": payment["id"],
            "receipt_url": url_for(
                "incident_receipt",
                qr_token=qr_token,
                payment_id=payment["id"],
            ),
        })

    client = get_razorpay_client()

    if client is None:
        connection.close()
        return jsonify({
            "success": False,
            "message": (
                "Payment service is unavailable."
            ),
        }), 503

    try:
        client.utility.verify_payment_signature({
            "razorpay_order_id":
                razorpay_order_id,
            "razorpay_payment_id":
                razorpay_payment_id,
            "razorpay_signature":
                razorpay_signature,
        })

        gateway_payment = client.payment.fetch(
            razorpay_payment_id
        )

        expected_amount = int(
            payment["charge"]
        ) * 100

        if (
            gateway_payment.get("status")
            == "authorized"
        ):
            client.payment.capture(
                razorpay_payment_id,
                expected_amount,
            )
            gateway_payment = client.payment.fetch(
                razorpay_payment_id
            )

    except Exception as error:
        connection.close()
        print("Razorpay verification error:", error)
        return jsonify({
            "success": False,
            "message": (
                "Payment verification failed."
            ),
        }), 400

    expected_amount = int(
        payment["charge"]
    ) * 100

    if (
        gateway_payment.get("order_id")
        != razorpay_order_id
        or int(
            gateway_payment.get("amount", 0)
        ) != expected_amount
        or gateway_payment.get("currency")
        != "INR"
        or gateway_payment.get("status")
        != "captured"
    ):
        connection.close()
        return jsonify({
            "success": False,
            "message": (
                "Payment has not been captured."
            ),
        }), 400

    gateway_method = gateway_payment.get(
        "method",
        "",
    )

    payment_method = {
        "upi": "Online / UPI",
        "card": "Card",
        "netbanking": "Online",
        "wallet": "Online",
    }.get(
        gateway_method,
        payment["payment_method"],
    )

    receipt_no = f"AUR-I-{payment['id']:05d}"

    try:
        connection.execute(
            """
            UPDATE incident_payments
            SET receipt_no = ?,
                payment_method = ?,
                payment_status = 'Paid',
                payment_reference = ?,
                gateway_payment_id = ?,
                gateway_signature = ?,
                paid_at =
                    datetime('now', 'localtime')
            WHERE id = ?
            """,
            (
                receipt_no,
                payment_method,
                razorpay_payment_id,
                razorpay_payment_id,
                razorpay_signature,
                payment["id"],
            ),
        )

        connection.execute(
            """
            UPDATE book_incidents
            SET status = 'Paid'
            WHERE id = ?
            """,
            (incident_id,),
        )

        connection.commit()

    except psycopg2.Error:
        connection.rollback()
        connection.close()
        return jsonify({
            "success": False,
            "message": (
                "Payment succeeded, but the local "
                "record could not be updated. "
                "Contact the librarian."
            ),
        }), 500

    connection.close()

    return jsonify({
        "success": True,
        "payment_id": payment["id"],
        "receipt_url": url_for(
            "incident_receipt",
            qr_token=qr_token,
            payment_id=payment["id"],
        ),
    })


# -------------------------------------------------
# INCIDENT RECEIPTS
# -------------------------------------------------

def get_incident_receipt(
    connection,
    payment_id,
):
    return connection.execute(
        """
        SELECT
            incident_payments.id,
            incident_payments.receipt_no,
            incident_payments.amount,
            incident_payments.payment_method,
            incident_payments.payment_status,
            incident_payments.payment_reference,
            incident_payments.cash_confirmed_by,
            incident_payments.paid_at,
            book_incidents.id AS incident_id,
            book_incidents.incident_type,
            book_incidents.description,
            books.title,
            books.author,
            members.id AS member_id,
            members.name,
            members.membership_id,
            members.enrollment_no,
            members.department,
            members.study_year,
            members.qr_token
        FROM incident_payments
        JOIN book_incidents
          ON book_incidents.id =
             incident_payments.incident_id
        JOIN books
          ON books.id = book_incidents.book_id
        JOIN members
          ON members.id = book_incidents.member_id
        WHERE incident_payments.id = ?
          AND incident_payments.payment_status = 'Paid'
        """,
        (payment_id,),
    ).fetchone()



def _receipt_value(receipt, key, default="-"):
    """Read a receipt field safely from sqlite3.Row or dict."""
    try:
        value = receipt[key]
    except (KeyError, IndexError, TypeError):
        value = default

    if value is None or value == "":
        return default

    return value


def _draw_aureon_pdf_logo(pdf, x, y, size=54):
    """Draw static/logo.png in a ReportLab PDF."""
    logo_path = os.path.join(
        app.static_folder or os.path.join(BASE_DIR, "static"),
        "logo.png",
    )

    if os.path.isfile(logo_path):
        try:
            pdf.drawImage(
                logo_path,
                x,
                y,
                width=size,
                height=size,
                preserveAspectRatio=True,
                anchor="c",
                mask="auto",
            )
            return
        except Exception:
            pass

    # Fallback mark only when the image cannot be read.
    pdf.setFillColor(HexColor("#FFFFFF"))
    pdf.roundRect(
        x,
        y,
        size,
        size,
        11,
        fill=1,
        stroke=0,
    )
    pdf.setFillColor(HexColor("#4338CA"))
    pdf.setFont("Helvetica-Bold", 24)
    pdf.drawCentredString(
        x + (size / 2),
        y + 17,
        "A",
    )


def _draw_pdf_text_lines(
    pdf,
    text_value,
    x,
    y,
    max_width,
    font_name="Helvetica",
    font_size=9,
    color="#37305F",
    line_height=12,
    max_lines=3,
):
    """Draw wrapped text and return the next y-coordinate."""
    text_value = str(text_value or "-")
    average_character_width = max(font_size * 0.52, 1)
    character_limit = max(
        12,
        int(max_width / average_character_width),
    )

    lines = wrap(
        text_value,
        width=character_limit,
    ) or ["-"]

    if max_lines:
        lines = lines[:max_lines]

    pdf.setFillColor(HexColor(color))
    pdf.setFont(font_name, font_size)

    for line in lines:
        pdf.drawString(x, y, line)
        y -= line_height

    return y


def _draw_pdf_receipt_row(
    pdf,
    label,
    value,
    x,
    y,
    width,
    row_height=38,
):
    """Draw one clean receipt information row."""
    pdf.setFillColor(HexColor("#FAF9FF"))
    pdf.setStrokeColor(HexColor("#E9E4F7"))
    pdf.roundRect(
        x,
        y - row_height,
        width,
        row_height,
        8,
        fill=1,
        stroke=1,
    )

    label_width = 150

    pdf.setFillColor(HexColor("#77729B"))
    pdf.setFont("Helvetica-Bold", 8)
    pdf.drawString(
        x + 13,
        y - 15,
        str(label).upper(),
    )

    _draw_pdf_text_lines(
        pdf,
        value,
        x + label_width,
        y - 15,
        width - label_width - 13,
        font_name="Helvetica-Bold",
        font_size=9,
        color="#312E81",
        line_height=11,
        max_lines=2,
    )

    return y - row_height - 7


def _draw_pdf_receipt_header(
    pdf,
    page_width,
    page_height,
    title,
    receipt_no,
):
    """Draw the purple Aureon receipt header with the college logo."""
    margin = 38
    card_width = page_width - (margin * 2)
    header_height = 122
    header_bottom = page_height - margin - header_height

    pdf.setFillColor(HexColor("#F5F3FF"))
    pdf.rect(
        0,
        0,
        page_width,
        page_height,
        fill=1,
        stroke=0,
    )

    pdf.setFillColor(white)
    pdf.setStrokeColor(HexColor("#DDD6FE"))
    pdf.roundRect(
        margin,
        38,
        card_width,
        page_height - 76,
        18,
        fill=1,
        stroke=1,
    )

    pdf.setFillColor(HexColor("#4338CA"))
    pdf.roundRect(
        margin,
        header_bottom,
        card_width,
        header_height,
        18,
        fill=1,
        stroke=0,
    )

    # Hide the lower rounded corners of the header.
    pdf.rect(
        margin,
        header_bottom,
        card_width,
        20,
        fill=1,
        stroke=0,
    )

    logo_size = 56
    logo_x = margin + 20
    logo_y = header_bottom + 40

    _draw_aureon_pdf_logo(
        pdf,
        logo_x,
        logo_y,
        logo_size,
    )

    text_x = logo_x + logo_size + 16

    pdf.setFillColor(HexColor("#DDD6FE"))
    pdf.setFont("Helvetica-Bold", 8)
    pdf.drawString(
        text_x,
        header_bottom + 91,
        "AUREON DIGITAL LIBRARY",
    )

    pdf.setFillColor(white)
    pdf.setFont("Helvetica-Bold", 20)
    pdf.drawString(
        text_x,
        header_bottom + 66,
        title,
    )

    pdf.setFillColor(HexColor("#E7E5FF"))
    pdf.setFont("Helvetica", 9)
    pdf.drawString(
        text_x,
        header_bottom + 48,
        "Rajaram Shinde Institute of Engineering and Technology",
    )

    badge_width = 70
    badge_x = margin + card_width - badge_width - 18
    badge_y = header_bottom + 74

    pdf.setFillColor(HexColor("#FFFFFF"))
    pdf.roundRect(
        badge_x,
        badge_y,
        badge_width,
        26,
        13,
        fill=1,
        stroke=0,
    )
    pdf.setFillColor(HexColor("#237454"))
    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawCentredString(
        badge_x + (badge_width / 2),
        badge_y + 9,
        "PAID",
    )

    content_x = margin + 22
    content_width = card_width - 44
    y = header_bottom - 22

    pdf.setFillColor(HexColor("#F7F5FF"))
    pdf.setStrokeColor(HexColor("#DED8F5"))
    pdf.roundRect(
        content_x,
        y - 42,
        content_width,
        42,
        10,
        fill=1,
        stroke=1,
    )

    pdf.setFillColor(HexColor("#77729B"))
    pdf.setFont("Helvetica-Bold", 8)
    pdf.drawString(
        content_x + 13,
        y - 17,
        "RECEIPT NUMBER",
    )

    pdf.setFillColor(HexColor("#4338CA"))
    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawRightString(
        content_x + content_width - 13,
        y - 17,
        str(receipt_no or "-"),
    )

    return {
        "margin": margin,
        "card_width": card_width,
        "content_x": content_x,
        "content_width": content_width,
        "y": y - 59,
    }


def _finish_pdf_receipt(
    pdf,
    page_width,
    amount,
    amount_label,
    receipt_no,
):
    """Draw the amount and computer-generated receipt footer."""
    margin = 38
    content_x = margin + 22
    content_width = page_width - (margin * 2) - 44

    total_y = 105

    pdf.setFillColor(HexColor("#4338CA"))
    pdf.roundRect(
        content_x,
        total_y,
        content_width,
        64,
        12,
        fill=1,
        stroke=0,
    )

    pdf.setFillColor(HexColor("#DDD6FE"))
    pdf.setFont("Helvetica-Bold", 8)
    pdf.drawCentredString(
        page_width / 2,
        total_y + 43,
        amount_label.upper(),
    )

    pdf.setFillColor(white)
    pdf.setFont("Helvetica-Bold", 22)
    pdf.drawCentredString(
        page_width / 2,
        total_y + 18,
        f"INR {amount}",
    )

    pdf.setFillColor(HexColor("#77729B"))
    pdf.setFont("Helvetica", 7.5)
    pdf.drawCentredString(
        page_width / 2,
        75,
        "This is a computer-generated receipt and does not require a physical signature.",
    )

    pdf.setFillColor(HexColor("#4338CA"))
    pdf.setFont("Helvetica-Bold", 8)
    pdf.drawCentredString(
        page_width / 2,
        60,
        f"Aureon Library Management System  |  {receipt_no}",
    )


def _build_incident_receipt_pdf(receipt):
    """Generate the downloadable Lost/Damaged PDF with the college logo."""
    pdf_buffer = BytesIO()
    pdf = canvas.Canvas(
        pdf_buffer,
        pagesize=A4,
    )

    page_width, page_height = A4
    receipt_no = _receipt_value(
        receipt,
        "receipt_no",
        "Incident Receipt",
    )

    pdf.setTitle(str(receipt_no))

    layout = _draw_pdf_receipt_header(
        pdf,
        page_width,
        page_height,
        "Lost / Damaged Book Receipt",
        receipt_no,
    )

    x = layout["content_x"]
    y = layout["y"]
    width = layout["content_width"]

    pdf.setFillColor(HexColor("#312E81"))
    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawString(x, y, "Member Information")
    y -= 17

    rows = [
        (
            "Member Name",
            _receipt_value(receipt, "name"),
        ),
        (
            "Membership ID",
            _receipt_value(receipt, "membership_id"),
        ),
        (
            "Enrollment Number",
            _receipt_value(receipt, "enrollment_no"),
        ),
        (
            "Department / Year",
            (
                f"{_receipt_value(receipt, 'department')} / "
                f"{_receipt_value(receipt, 'study_year')}"
            ),
        ),
    ]

    for label, value in rows:
        y = _draw_pdf_receipt_row(
            pdf,
            label,
            value,
            x,
            y,
            width,
        )

    y -= 2
    pdf.setFillColor(HexColor("#312E81"))
    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawString(x, y, "Incident and Payment Details")
    y -= 17

    rows = [
        (
            "Book",
            _receipt_value(receipt, "title"),
        ),
        (
            "Author",
            _receipt_value(receipt, "author"),
        ),
        (
            "Incident Type",
            _receipt_value(receipt, "incident_type"),
        ),
        (
            "Description",
            _receipt_value(
                receipt,
                "description",
                "No description provided.",
            ),
        ),
        (
            "Payment Method",
            _receipt_value(receipt, "payment_method"),
        ),
        (
            "Payment Reference",
            _receipt_value(receipt, "payment_reference"),
        ),
        (
            "Paid Date and Time",
            _receipt_value(receipt, "paid_at"),
        ),
    ]

    for label, value in rows:
        y = _draw_pdf_receipt_row(
            pdf,
            label,
            value,
            x,
            y,
            width,
            row_height=42 if label == "Description" else 38,
        )

    if (
        str(
            _receipt_value(
                receipt,
                "payment_method",
                "",
            )
        ).lower()
        == "cash"
    ):
        y = _draw_pdf_receipt_row(
            pdf,
            "Cash Confirmed By",
            _receipt_value(
                receipt,
                "cash_confirmed_by",
                "Librarian",
            ),
            x,
            y,
            width,
        )

    _finish_pdf_receipt(
        pdf,
        page_width,
        _receipt_value(receipt, "amount", "0"),
        "Lost / Damaged Charge Paid",
        receipt_no,
    )

    pdf.save()
    pdf_buffer.seek(0)
    return pdf_buffer

@app.route(
    "/scan/<qr_token>/incident-receipt/"
    "<int:payment_id>"
)
def incident_receipt(
    qr_token,
    payment_id,
):
    connection = get_db_connection()

    receipt = get_incident_receipt(
        connection,
        payment_id,
    )

    connection.close()

    if (
        receipt is None
        or receipt["qr_token"] != qr_token
    ):
        return "Receipt was not found.", 404

    return render_template(
        "incident_receipt.html",
        receipt=receipt,
        qr_token=qr_token,
    )


@app.route(
    "/scan/<qr_token>/incident-receipt/"
    "<int:payment_id>/download"
)
def download_incident_receipt(
    qr_token,
    payment_id,
):
    connection = get_db_connection()

    receipt = get_incident_receipt(
        connection,
        payment_id,
    )

    connection.close()

    if (
        receipt is None
        or receipt["qr_token"] != qr_token
    ):
        return "Receipt was not found.", 404

    return send_file(
        _build_incident_receipt_pdf(receipt),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=(
            f"{receipt['receipt_no']}.pdf"
        ),
    )


# -------------------------------------------------
# MEMBER REPORT EXPORT
# -------------------------------------------------

@app.route("/scan/<qr_token>/report/csv")
def member_incident_report_csv(qr_token):
    connection = get_db_connection()

    member = get_member_by_qr_token(
        connection,
        qr_token,
    )

    if member is None:
        connection.close()
        return "Invalid QR code.", 404

    history = _get_member_history(
        connection,
        member["id"],
    )

    fine_records = _get_fine_records(
        connection,
        member["id"],
    )

    incident_records = get_incident_records(
        connection,
        member["id"],
    )

    connection.close()

    csv_buffer = StringIO()
    writer = csv.writer(csv_buffer)

    writer.writerow([
        "Aureon Member Library Report",
    ])
    writer.writerow([
        "Member Name",
        member["name"],
    ])
    writer.writerow([
        "Membership ID",
        member["membership_id"] or "",
    ])
    writer.writerow([
        "Enrollment Number",
        member["enrollment_no"] or "",
    ])
    writer.writerow([
        "Department",
        member["department"] or "",
    ])
    writer.writerow([
        "Study Year",
        member["study_year"] or "",
    ])

    writer.writerow([])
    writer.writerow(["Borrowing History"])
    writer.writerow([
        "Book",
        "Author",
        "Issue Date",
        "Due Date",
        "Return Date",
        "Status",
        "Fine",
    ])

    for item in history:
        writer.writerow([
            item["title"],
            item["author"],
            item["issue_date"],
            item["due_date"],
            item["return_date"] or "",
            item["status"],
            item["fine"],
        ])

    writer.writerow([])
    writer.writerow(["Fine Payments"])
    writer.writerow([
        "Book",
        "Fine",
        "Payment Status",
        "Payment Method",
        "Receipt Number",
        "Paid At",
    ])

    for fine in fine_records:
        writer.writerow([
            fine["title"],
            fine["fine"],
            (
                "Paid"
                if fine["payment_id"]
                else "Pending"
            ),
            fine["payment_method"] or "",
            fine["receipt_no"] or "",
            fine["paid_at"] or "",
        ])

    writer.writerow([])
    writer.writerow(["Lost / Damaged Reports"])
    writer.writerow([
        "Book",
        "Incident",
        "Description",
        "Date Noticed",
        "Reported At",
        "Charge",
        "Incident Status",
        "Payment Method",
        "Payment Status",
        "Receipt Number",
        "Paid At",
    ])

    for incident in incident_records:
        writer.writerow([
            incident["title"],
            incident["incident_type"],
            incident["description"] or "",
            incident["noticed_date"] or "",
            incident["reported_at"] or "",
            incident["charge"],
            incident["status"],
            incident["payment_method"] or "",
            incident["payment_status"] or "",
            incident["receipt_no"] or "",
            incident["paid_at"] or "",
        ])

    filename = (
        f"{member['membership_id'] or member['id']}"
        "-library-report.csv"
    )

    return Response(
        csv_buffer.getvalue(),
        mimetype="text/csv",
        headers={
            "Content-Disposition":
                f'attachment; filename="{filename}"'
        },
    )


@app.route("/scan/<qr_token>/report/pdf")
def member_incident_report_pdf(qr_token):
    connection = get_db_connection()

    member = get_member_by_qr_token(
        connection,
        qr_token,
    )

    if member is None:
        connection.close()
        return "Invalid QR code.", 404

    history = _get_member_history(
        connection,
        member["id"],
    )

    fine_records = _get_fine_records(
        connection,
        member["id"],
    )

    incident_records = get_incident_records(
        connection,
        member["id"],
    )

    connection.close()

    pdf_buffer = BytesIO()

    pdf = canvas.Canvas(
        pdf_buffer,
        pagesize=A4,
    )

    page_width, page_height = A4
    left_margin = 42
    y_position = page_height - 48

    def ensure_space(required_height=60):
        nonlocal y_position

        if y_position < required_height:
            pdf.showPage()
            y_position = page_height - 48

    def draw_wrapped(
        text_value,
        font_name="Helvetica",
        font_size=9,
        indent=0,
        width=92,
    ):
        nonlocal y_position

        lines = wrap(
            str(text_value),
            width=width,
        ) or ["-"]

        pdf.setFont(
            font_name,
            font_size,
        )

        for line in lines:
            ensure_space(55)
            pdf.drawString(
                left_margin + indent,
                y_position,
                line,
            )
            y_position -= font_size + 4

    pdf.setTitle(
        f"{member['membership_id']} Library Report"
    )

    pdf.setFont(
        "Helvetica-Bold",
        18,
    )
    pdf.drawString(
        left_margin,
        y_position,
        "AUREON LIBRARY REPORT",
    )
    y_position -= 28

    draw_wrapped(
        f"Member: {member['name']}",
        "Helvetica-Bold",
        10,
    )
    draw_wrapped(
        "Membership ID: "
        f"{member['membership_id'] or '-'}",
        width=75,
    )
    draw_wrapped(
        "Enrollment Number: "
        f"{member['enrollment_no'] or '-'}",
        width=75,
    )
    draw_wrapped(
        "Department: "
        f"{member['department'] or '-'}",
        width=75,
    )
    y_position -= 12

    ensure_space(85)
    pdf.setFont(
        "Helvetica-Bold",
        13,
    )
    pdf.drawString(
        left_margin,
        y_position,
        "Borrowing History",
    )
    y_position -= 20

    if history:
        for item in history:
            ensure_space(85)

            draw_wrapped(
                item["title"],
                "Helvetica-Bold",
                10,
                width=72,
            )

            draw_wrapped(
                (
                    f"Author: {item['author']} | "
                    f"Issued: {item['issue_date']} | "
                    f"Due: {item['due_date']} | "
                    f"Returned: "
                    f"{item['return_date'] or '-'} | "
                    f"Status: {item['status']} | "
                    f"Fine: INR {item['fine']}"
                ),
                indent=10,
                width=92,
            )
            y_position -= 7
    else:
        draw_wrapped(
            "No borrowing history.",
        )

    y_position -= 10
    ensure_space(85)

    pdf.setFont(
        "Helvetica-Bold",
        13,
    )
    pdf.drawString(
        left_margin,
        y_position,
        "Fine Payment Records",
    )
    y_position -= 20

    if fine_records:
        for fine in fine_records:
            ensure_space(70)

            status_text = (
                "Paid"
                if fine["payment_id"]
                else "Pending"
            )

            draw_wrapped(
                (
                    f"{fine['title']} | "
                    f"Fine: INR {fine['fine']} | "
                    f"Status: {status_text} | "
                    f"Method: "
                    f"{fine['payment_method'] or '-'} | "
                    f"Receipt: "
                    f"{fine['receipt_no'] or '-'}"
                ),
                width=92,
            )
            y_position -= 6
    else:
        draw_wrapped(
            "No fine payment records.",
        )

    y_position -= 10
    ensure_space(85)

    pdf.setFont(
        "Helvetica-Bold",
        13,
    )
    pdf.drawString(
        left_margin,
        y_position,
        "Lost / Damaged Reports",
    )
    y_position -= 20

    if incident_records:
        for incident in incident_records:
            ensure_space(95)

            draw_wrapped(
                (
                    f"{incident['incident_type']}: "
                    f"{incident['title']}"
                ),
                "Helvetica-Bold",
                10,
                width=72,
            )

            draw_wrapped(
                (
                    f"Charge: INR {incident['charge']} | "
                    f"Incident Status: "
                    f"{incident['status']} | "
                    f"Payment Status: "
                    f"{incident['payment_status'] or 'Pending'} | "
                    f"Method: "
                    f"{incident['payment_method'] or '-'} | "
                    f"Receipt: "
                    f"{incident['receipt_no'] or '-'} | "
                    f"Paid At: "
                    f"{incident['paid_at'] or '-'}"
                ),
                indent=10,
                width=92,
            )

            if incident["description"]:
                draw_wrapped(
                    "Description: "
                    f"{incident['description']}",
                    indent=10,
                    width=92,
                )

            y_position -= 7
    else:
        draw_wrapped(
            "No lost or damaged reports.",
        )

    pdf.save()
    pdf_buffer.seek(0)

    filename = (
        f"{member['membership_id'] or member['id']}"
        "-library-report.pdf"
    )

    return send_file(
        pdf_buffer,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=filename,
    )


# -------------------------------------------------
# FINE PAYMENTS
# -------------------------------------------------

def _find_payable_transaction(
    connection,
    member_id,
    transaction_id,
):
    return connection.execute(
        """
        SELECT
            transactions.id,
            transactions.member_id,
            transactions.fine,
            books.title,
            members.name
        FROM transactions
        JOIN books
          ON books.id = transactions.book_id
        JOIN members
          ON members.id = transactions.member_id
        WHERE transactions.id = ?
          AND transactions.member_id = ?
        """,
        (
            transaction_id,
            member_id,
        ),
    ).fetchone()


def _create_fine_payment(
    connection,
    transaction,
    payment_method,
    payment_reference,
):
    existing_payment = connection.execute(
        """
        SELECT id
        FROM fine_payments
        WHERE transaction_id = ?
        """,
        (transaction["id"],),
    ).fetchone()

    if existing_payment is not None:
        return None, "already_paid"

    receipt_no = (
        f"AUR-F-{transaction['id']:05d}"
    )

    cursor = connection.execute(
        """
        INSERT INTO fine_payments (
            transaction_id,
            receipt_no,
            amount,
            payment_method,
            payment_reference,
            paid_at
        )
        VALUES (
            ?, ?, ?, ?, ?,
            datetime('now', 'localtime')
        )
        """,
        (
            transaction["id"],
            receipt_no,
            transaction["fine"],
            payment_method,
            payment_reference,
        ),
    )

    return cursor.lastrowid, None


@app.route(
    "/member/<int:member_id>/pay-fine/"
    "<int:transaction_id>",
    methods=["POST"],
)
@login_required
def pay_member_fine(
    member_id,
    transaction_id,
):
    connection = get_db_connection()

    transaction = _find_payable_transaction(
        connection,
        member_id,
        transaction_id,
    )

    if (
        transaction is None
        or transaction["fine"] <= 0
    ):
        connection.close()
        flash(
            "Fine transaction was not found.",
            "danger",
        )
        return redirect(
            url_for(
                "member_profile",
                member_id=member_id,
            )
        )

    payment_method = request.form.get(
        "payment_method",
        "",
    ).strip()
    payment_reference = request.form.get(
        "payment_reference",
        "",
    ).strip()
    want_receipt = (
        request.form.get("want_receipt")
        == "yes"
    )

    if payment_method not in {
        "Cash",
        "Online",
        "Card",
    }:
        connection.close()
        flash(
            "Please select a valid "
            "payment method.",
            "danger",
        )
        return redirect(
            url_for(
                "member_profile",
                member_id=member_id,
            )
        )

    try:
        payment_id, payment_error = (
            _create_fine_payment(
                connection,
                transaction,
                payment_method,
                payment_reference,
            )
        )

        if payment_error == "already_paid":
            connection.close()
            flash(
                "This fine has already been paid.",
                "warning",
            )
            return redirect(
                url_for(
                    "member_profile",
                    member_id=member_id,
                )
            )

        connection.commit()

    except psycopg2.IntegrityError:
        connection.rollback()
        connection.close()
        flash(
            "This fine has already been paid.",
            "warning",
        )
        return redirect(
            url_for(
                "member_profile",
                member_id=member_id,
            )
        )

    connection.close()
    flash(
        "Fine payment recorded successfully.",
        "success",
    )

    if want_receipt:
        return redirect(
            url_for(
                "fine_receipt",
                payment_id=payment_id,
            )
        )

    return redirect(
        url_for(
            "member_profile",
            member_id=member_id,
        )
    )


@app.route(
    "/member/<int:member_id>/qr-pay-fine/"
    "<int:transaction_id>",
    methods=["POST"],
)
def pay_qr_fine(
    member_id,
    transaction_id,
):
    connection = get_db_connection()

    transaction = _find_payable_transaction(
        connection,
        member_id,
        transaction_id,
    )

    if (
        transaction is None
        or transaction["fine"] <= 0
    ):
        connection.close()
        return redirect(
            url_for(
                "member_qr_history",
                member_id=member_id,
            )
        )

    payment_method = request.form.get(
        "payment_method",
        "",
    ).strip()
    payment_reference = request.form.get(
        "payment_reference",
        "",
    ).strip()
    want_receipt = (
        request.form.get("want_receipt")
        == "yes"
    )

    if payment_method not in {
        "Cash",
        "Online",
        "Card",
    }:
        connection.close()
        return redirect(
            url_for(
                "member_qr_history",
                member_id=member_id,
            )
        )

    try:
        payment_id, payment_error = (
            _create_fine_payment(
                connection,
                transaction,
                payment_method,
                payment_reference,
            )
        )

        if payment_error == "already_paid":
            connection.close()
            return redirect(
                url_for(
                    "member_qr_history",
                    member_id=member_id,
                )
            )

        connection.commit()

    except psycopg2.IntegrityError:
        connection.rollback()
        connection.close()
        return redirect(
            url_for(
                "member_qr_history",
                member_id=member_id,
            )
        )

    connection.close()

    if want_receipt:
        return redirect(
            url_for(
                "qr_fine_receipt",
                member_id=member_id,
                payment_id=payment_id,
            )
        )

    return redirect(
        url_for(
            "member_qr_history",
            member_id=member_id,
        )
    )


# -------------------------------------------------
# RECEIPTS
# -------------------------------------------------

def get_fine_receipt(
    connection,
    payment_id,
):
    return connection.execute(
        """
        SELECT
            fine_payments.id,
            fine_payments.receipt_no,
            fine_payments.amount,
            fine_payments.payment_method,
            fine_payments.payment_reference,
            fine_payments.paid_at,
            transactions.issue_date,
            transactions.due_date,
            transactions.return_date,
            books.title,
            books.author,
            members.id AS member_id,
            members.name,
            members.membership_id,
            members.enrollment_no,
            members.department,
            members.study_year
        FROM fine_payments
        JOIN transactions
          ON transactions.id =
             fine_payments.transaction_id
        JOIN books
          ON books.id = transactions.book_id
        JOIN members
          ON members.id = transactions.member_id
        WHERE fine_payments.id = ?
        """,
        (payment_id,),
    ).fetchone()


def _build_receipt_pdf(receipt):
    """Generate the downloadable Fine PDF in the receipt-page style."""
    pdf_buffer = BytesIO()
    pdf = canvas.Canvas(
        pdf_buffer,
        pagesize=A4,
    )

    page_width, page_height = A4
    receipt_no = _receipt_value(
        receipt,
        "receipt_no",
        "Fine Receipt",
    )

    pdf.setTitle(str(receipt_no))

    layout = _draw_pdf_receipt_header(
        pdf,
        page_width,
        page_height,
        "Fine Payment Receipt",
        receipt_no,
    )

    x = layout["content_x"]
    y = layout["y"]
    width = layout["content_width"]

    pdf.setFillColor(HexColor("#312E81"))
    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawString(x, y, "Member Information")
    y -= 17

    rows = [
        (
            "Member Name",
            _receipt_value(receipt, "name"),
        ),
        (
            "Membership ID",
            _receipt_value(receipt, "membership_id"),
        ),
        (
            "Enrollment Number",
            _receipt_value(receipt, "enrollment_no"),
        ),
        (
            "Department / Year",
            (
                f"{_receipt_value(receipt, 'department')} / "
                f"{_receipt_value(receipt, 'study_year')}"
            ),
        ),
    ]

    for label, value in rows:
        y = _draw_pdf_receipt_row(
            pdf,
            label,
            value,
            x,
            y,
            width,
        )

    y -= 2
    pdf.setFillColor(HexColor("#312E81"))
    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawString(x, y, "Book and Payment Details")
    y -= 17

    rows = [
        (
            "Book",
            _receipt_value(receipt, "title"),
        ),
        (
            "Author",
            _receipt_value(receipt, "author"),
        ),
        (
            "Issue / Due / Return",
            (
                f"{_receipt_value(receipt, 'issue_date')}  /  "
                f"{_receipt_value(receipt, 'due_date')}  /  "
                f"{_receipt_value(receipt, 'return_date')}"
            ),
        ),
        (
            "Payment Method",
            _receipt_value(receipt, "payment_method"),
        ),
        (
            "Payment Reference",
            _receipt_value(receipt, "payment_reference"),
        ),
        (
            "Payment Date",
            _receipt_value(receipt, "paid_at"),
        ),
    ]

    for label, value in rows:
        y = _draw_pdf_receipt_row(
            pdf,
            label,
            value,
            x,
            y,
            width,
        )

    _finish_pdf_receipt(
        pdf,
        page_width,
        _receipt_value(receipt, "amount", "0"),
        "Fine Amount Paid",
        receipt_no,
    )

    pdf.save()
    pdf_buffer.seek(0)
    return pdf_buffer

@app.route(
    "/fine-receipt/<int:payment_id>"
)
@login_required
def fine_receipt(payment_id):
    connection = get_db_connection()
    receipt = get_fine_receipt(
        connection,
        payment_id,
    )
    connection.close()

    if receipt is None:
        flash(
            "Receipt was not found.",
            "danger",
        )
        return redirect(url_for("members"))

    return render_template(
        "fine_receipt.html",
        receipt=receipt,
        qr_view=False,
    )


@app.route(
    "/fine-receipt/<int:payment_id>/download"
)
@login_required
def download_fine_receipt(payment_id):
    connection = get_db_connection()
    receipt = get_fine_receipt(
        connection,
        payment_id,
    )
    connection.close()

    if receipt is None:
        flash(
            "Receipt was not found.",
            "danger",
        )
        return redirect(url_for("members"))

    return send_file(
        _build_receipt_pdf(receipt),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=(
            f"{receipt['receipt_no']}.pdf"
        ),
    )


@app.route(
    "/member/<int:member_id>/qr-receipt/"
    "<int:payment_id>"
)
def qr_fine_receipt(
    member_id,
    payment_id,
):
    connection = get_db_connection()
    receipt = get_fine_receipt(
        connection,
        payment_id,
    )
    connection.close()

    if (
        receipt is None
        or receipt["member_id"] != member_id
    ):
        return "Receipt was not found.", 404

    return render_template(
        "fine_receipt.html",
        receipt=receipt,
        qr_view=True,
    )


@app.route(
    "/member/<int:member_id>/qr-receipt/"
    "<int:payment_id>/download"
)
def download_qr_fine_receipt(
    member_id,
    payment_id,
):
    connection = get_db_connection()
    receipt = get_fine_receipt(
        connection,
        payment_id,
    )
    connection.close()

    if (
        receipt is None
        or receipt["member_id"] != member_id
    ):
        return "Receipt was not found.", 404

    return send_file(
        _build_receipt_pdf(receipt),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=(
            f"{receipt['receipt_no']}.pdf"
        ),
    )




# -------------------------------------------------
# SECURE STUDENT DIGITAL CARD
# -------------------------------------------------

@app.route(
    "/scan/<qr_token>/setup",
    methods=["GET", "POST"],
)
def member_card_setup(qr_token):
    connection = get_db_connection()
    member = get_member_by_qr_token(
        connection,
        qr_token,
    )

    if member is None:
        connection.close()
        return "Invalid or expired QR code.", 404

    if not member["card_portal_enabled"]:
        connection.close()
        return (
            "This Digital Library Card has been disabled. "
            "Please contact the Librarian.",
            403,
        )

    if (
        member["card_password_hash"]
        and not member["card_reset_required"]
    ):
        connection.close()
        return redirect(
            url_for(
                "member_card_login",
                qr_token=qr_token,
            )
        )

    if request.method == "POST":
        membership_id = request.form.get(
            "membership_id",
            "",
        ).strip()
        new_password = request.form.get(
            "new_password",
            "",
        )
        confirm_password = request.form.get(
            "confirm_password",
            "",
        )

        if membership_id != member["membership_id"]:
            connection.close()
            flash(
                "The Membership ID does not match this QR card.",
                "danger",
            )
            return redirect(
                url_for(
                    "member_card_setup",
                    qr_token=qr_token,
                )
            )

        if not _password_is_strong(new_password):
            connection.close()
            flash(
                "Use at least 8 characters with uppercase, "
                "lowercase and one number.",
                "danger",
            )
            return redirect(
                url_for(
                    "member_card_setup",
                    qr_token=qr_token,
                )
            )

        if new_password != confirm_password:
            connection.close()
            flash(
                "The two passwords do not match.",
                "danger",
            )
            return redirect(
                url_for(
                    "member_card_setup",
                    qr_token=qr_token,
                )
            )

        password_hash = generate_password_hash(
            new_password,
        )

        connection.execute(
    """
    UPDATE members
    SET card_password_hash = ?,
        card_password_created_at =
            COALESCE(
                card_password_created_at,
                CURRENT_TIMESTAMP::text
            ),
        card_password_updated_at =
            CURRENT_TIMESTAMP::text,
        card_failed_attempts = 0,
        card_locked_until = NULL,
        card_reset_required = 0,
        card_session_version =
            COALESCE(card_session_version, 1) + 1,
        card_last_login =
            CURRENT_TIMESTAMP::text
    WHERE id = ?
    """,
    (
        password_hash,
        member["id"],
    ),
)

        updated_member = connection.execute(
            """
            SELECT *
            FROM members
            WHERE id = ?
            """,
            (member["id"],),
        ).fetchone()

        _create_member_notification(
            connection,
            member["id"],
            "General",
            "Digital Card activated",
            (
                "Your password-protected Aureon Digital "
                "Library Card was activated successfully."
            ),
            unique_key=(
                f"member:{member['id']}:card-activated"
            ),
        )

        connection.commit()
        connection.close()

        session.clear()
        session["role"] = "member"
        session["member_id"] = updated_member["id"]
        session["member_qr_token"] = qr_token
        session["card_session_version"] = (
            updated_member["card_session_version"]
        )
        session["card_last_activity"] = time.time()

        flash(
            "Your Digital Library Card is now active.",
            "success",
        )
        return redirect(url_for("member_portal"))

    connection.close()

    return render_template(
        "member_card_setup.html",
        member=member,
    )


@app.route(
    "/scan/<qr_token>/login",
    methods=["GET", "POST"],
)
def member_card_login(qr_token):
    connection = get_db_connection()
    member = get_member_by_qr_token(
        connection,
        qr_token,
    )

    if member is None:
        connection.close()
        return "Invalid or expired QR code.", 404

    if not member["card_portal_enabled"]:
        connection.close()
        return (
            "This Digital Library Card has been disabled. "
            "Please contact the Librarian.",
            403,
        )

    if (
        not member["card_password_hash"]
        or member["card_reset_required"]
    ):
        connection.close()
        return redirect(
            url_for(
                "member_card_setup",
                qr_token=qr_token,
            )
        )

    locked_until = _parse_local_datetime(
        member["card_locked_until"]
    )
    now = datetime.now()
    lock_message = None

    if locked_until and locked_until > now:
        remaining_minutes = max(
            1,
            int(
                (
                    locked_until - now
                ).total_seconds() // 60
            ) + 1,
        )
        lock_message = (
            "Too many incorrect attempts. Try again in "
            f"about {remaining_minutes} minute(s), or ask "
            "the Librarian to unlock the card."
        )
    elif locked_until:
        connection.execute(
            """
            UPDATE members
            SET card_failed_attempts = 0,
                card_locked_until = NULL
            WHERE id = ?
            """,
            (member["id"],),
        )
        connection.commit()
        member = get_member_by_qr_token(
            connection,
            qr_token,
        )

    if request.method == "POST":
        if lock_message:
            connection.close()
            flash(lock_message, "danger")
            return redirect(
                url_for(
                    "member_card_login",
                    qr_token=qr_token,
                )
            )

        membership_id = request.form.get(
            "membership_id",
            "",
        ).strip()
        password = request.form.get(
            "password",
            "",
        )

        credentials_are_valid = (
            secrets.compare_digest(
                membership_id,
                member["membership_id"] or "",
            )
            and check_password_hash(
                member["card_password_hash"],
                password,
            )
        )

        if not credentials_are_valid:
            failed_attempts = (
                int(member["card_failed_attempts"] or 0)
                + 1
            )

            if failed_attempts >= CARD_MAX_FAILED_ATTEMPTS:
                lock_until = (
                    datetime.now()
                    + timedelta(
                        minutes=CARD_LOCK_MINUTES
                    )
                ).replace(microsecond=0)

                connection.execute(
                    """
                    UPDATE members
                    SET card_failed_attempts = ?,
                        card_locked_until = ?
                    WHERE id = ?
                    """,
                    (
                        failed_attempts,
                        lock_until.isoformat(sep=" "),
                        member["id"],
                    ),
                )
                connection.commit()
                connection.close()

                flash(
                    "Digital Card locked for 15 minutes "
                    "after repeated incorrect attempts.",
                    "danger",
                )
                return redirect(
                    url_for(
                        "member_card_login",
                        qr_token=qr_token,
                    )
                )

            connection.execute(
                """
                UPDATE members
                SET card_failed_attempts = ?
                WHERE id = ?
                """,
                (
                    failed_attempts,
                    member["id"],
                ),
            )
            connection.commit()
            connection.close()

            attempts_left = (
                CARD_MAX_FAILED_ATTEMPTS
                - failed_attempts
            )
            flash(
                "Incorrect Membership ID or password. "
                f"{attempts_left} attempt(s) remaining.",
                "danger",
            )
            return redirect(
                url_for(
                    "member_card_login",
                    qr_token=qr_token,
                )
            )

        connection.execute(
            """
            UPDATE members
            SET card_failed_attempts = 0,
                card_locked_until = NULL,
                card_last_login =
                    datetime('now', 'localtime')
            WHERE id = ?
            """,
            (member["id"],),
        )
        connection.commit()

        member = get_member_by_qr_token(
            connection,
            qr_token,
        )
        connection.close()

        session.clear()
        session["role"] = "member"
        session["member_id"] = member["id"]
        session["member_qr_token"] = qr_token
        session["card_session_version"] = (
            member["card_session_version"]
        )
        session["card_last_activity"] = time.time()

        flash(
            "Welcome to your Digital Library Card.",
            "success",
        )
        return redirect(url_for("member_portal"))

    connection.close()

    return render_template(
        "member_card_login.html",
        member=member,
        allow_setup=False,
        lock_message=lock_message,
    )


@app.route("/member-card/logout")
def member_card_logout():
    qr_token = session.get("member_qr_token")
    session.clear()

    flash(
        "You have logged out from your Digital Library Card.",
        "success",
    )

    if qr_token:
        return redirect(
            url_for(
                "member_card_login",
                qr_token=qr_token,
            )
        )

    return redirect(url_for("login"))


def _get_member_portal_information(connection, member):
    member_id = member["id"]
    today = date.today()

    issued_rows = connection.execute(
        """
        SELECT
            transactions.id AS transaction_id,
            transactions.issue_date,
            transactions.due_date,
            transactions.fine,
            books.id AS book_id,
            books.title,
            books.author,
            books.cover_url
        FROM transactions
        JOIN books
          ON books.id = transactions.book_id
        WHERE transactions.member_id = ?
          AND transactions.status = 'Issued'
        ORDER BY transactions.due_date
        """,
        (member_id,),
    ).fetchall()

    issued_books = []

    for row in issued_rows:
        item = dict(row)

        try:
            due_date = date.fromisoformat(
                row["due_date"]
            )
            day_difference = (
                due_date - today
            ).days
        except (TypeError, ValueError):
            day_difference = 0

        item["is_overdue"] = (
            day_difference < 0
        )
        item["days_remaining"] = max(
            day_difference,
            0,
        )
        item["overdue_days"] = max(
            -day_difference,
            0,
        )
        item["current_fine"] = (
            FINE_AMOUNT
            if day_difference < 0
            else 0
        )
        issued_books.append(item)

    return_rows = connection.execute(
        """
        SELECT
            transactions.id AS transaction_id,
            transactions.issue_date,
            transactions.due_date,
            transactions.return_date,
            transactions.fine,
            books.title,
            books.author,
            fine_payments.receipt_no,
            CASE
                WHEN fine_payments.id IS NOT NULL
                THEN 'Paid'
                WHEN fine_payment_requests.payment_status
                     IS NOT NULL
                THEN fine_payment_requests.payment_status
                WHEN transactions.fine > 0
                THEN 'Payment Pending'
                ELSE 'No Penalty'
            END AS payment_status
        FROM transactions
        JOIN books
          ON books.id = transactions.book_id
        LEFT JOIN fine_payments
          ON fine_payments.transaction_id =
             transactions.id
        LEFT JOIN fine_payment_requests
          ON fine_payment_requests.transaction_id =
             transactions.id
        WHERE transactions.member_id = ?
          AND transactions.status = 'Returned'
        ORDER BY transactions.id DESC
        """,
        (member_id,),
    ).fetchall()

    return_records = []

    for row in return_rows:
        item = dict(row)

        try:
            item["was_late"] = (
                date.fromisoformat(
                    row["return_date"]
                )
                > date.fromisoformat(
                    row["due_date"]
                )
            )
        except (TypeError, ValueError):
            item["was_late"] = False

        return_records.append(item)

    fine_rows = connection.execute(
        """
        SELECT
            transactions.id AS transaction_id,
            transactions.fine,
            transactions.return_date,
            books.title,
            fine_payments.id AS payment_id,
            fine_payments.receipt_no,
            CASE
                WHEN fine_payments.id IS NOT NULL
                THEN 'Paid'
                WHEN fine_payment_requests.payment_status
                     IS NOT NULL
                THEN fine_payment_requests.payment_status
                ELSE 'Payment Pending'
            END AS payment_status
        FROM transactions
        JOIN books
          ON books.id = transactions.book_id
        LEFT JOIN fine_payments
          ON fine_payments.transaction_id =
             transactions.id
        LEFT JOIN fine_payment_requests
          ON fine_payment_requests.transaction_id =
             transactions.id
        WHERE transactions.member_id = ?
          AND transactions.fine > 0
        ORDER BY transactions.id DESC
        """,
        (member_id,),
    ).fetchall()

    fine_records = [
        dict(row)
        for row in fine_rows
    ]

    incident_records = [
        dict(row)
        for row in get_incident_records(
            connection,
            member_id,
        )
    ]

    notification_rows = connection.execute(
        """
        SELECT
            notifications.id AS notification_id,
            notifications.category,
            notifications.priority,
            notifications.title,
            notifications.message,
            notifications.created_at,
            CASE
                WHEN notification_reads.id IS NULL
                THEN 0
                ELSE 1
            END AS is_read
        FROM notifications
        LEFT JOIN notification_reads
          ON notification_reads.notification_id =
             notifications.id
         AND notification_reads.member_id = ?
        WHERE notifications.is_active = 1
          AND (
                notifications.member_id = ?
                OR notifications.recipient_type = 'all'
              )
          AND (
                notifications.expires_at IS NULL
                OR TRIM(notifications.expires_at) = ''
                OR date(notifications.expires_at)
                   >= date('now', 'localtime')
              )
        ORDER BY
            CASE notifications.priority
                WHEN 'Urgent' THEN 1
                WHEN 'Important' THEN 2
                ELSE 3
            END,
            notifications.id DESC
        """,
        (
            member_id,
            member_id,
        ),
    ).fetchall()

    icon_map = {
        "Book Issue": "▤",
        "Book Return": "↩",
        "Due Date": "◷",
        "Overdue": "⚠",
        "Penalty": "₹",
        "Lost or Damaged": "!",
        "Payment": "✓",
        "Library Announcement": "◉",
        "General": "◉",
    }

    notifications = []

    for row in notification_rows:
        item = dict(row)
        item["icon"] = icon_map.get(
            item["category"],
            "◉",
        )
        notifications.append(item)

    unpaid_fine = connection.execute(
        """
        SELECT COALESCE(
            SUM(transactions.fine),
            0
        )
        FROM transactions
        LEFT JOIN fine_payments
          ON fine_payments.transaction_id =
             transactions.id
        WHERE transactions.member_id = ?
          AND transactions.fine > 0
          AND fine_payments.id IS NULL
        """,
        (member_id,),
    ).fetchone()[0]

    unpaid_incident_charge = connection.execute(
        """
        SELECT COALESCE(
            SUM(book_incidents.charge),
            0
        )
        FROM book_incidents
        LEFT JOIN incident_payments
          ON incident_payments.incident_id =
             book_incidents.id
        WHERE book_incidents.member_id = ?
          AND book_incidents.status = 'Approved'
          AND book_incidents.charge > 0
          AND (
                incident_payments.id IS NULL
                OR incident_payments.payment_status != 'Paid'
              )
        """,
        (member_id,),
    ).fetchone()[0]

    returned_count = connection.execute(
        """
        SELECT COUNT(*)
        FROM transactions
        WHERE member_id = ?
          AND status = 'Returned'
        """,
        (member_id,),
    ).fetchone()[0]

    unread_notifications = sum(
        1
        for notification in notifications
        if not notification["is_read"]
    )

    active_overdue_fine = sum(
        int(book.get("current_fine", 0) or 0)
        for book in issued_books
        if book.get("is_overdue")
    )

    portal_summary = {
        "issued_count": len(issued_books),
        "returned_count": returned_count,
        "pending_amount": (
            int(unpaid_fine or 0)
            + int(unpaid_incident_charge or 0)
            + int(active_overdue_fine or 0)
        ),
        "unread_notifications":
            unread_notifications,
    }

    next_due_book = (
        issued_books[0]
        if issued_books
        else None
    )

    return {
        "issued_books": issued_books,
        "return_records": return_records,
        "fine_records": fine_records,
        "incident_records": incident_records,
        "notifications": notifications,
        "portal_summary": portal_summary,
        "next_due_book": next_due_book,
    }

@app.route("/member-portal")
@member_card_required
def member_portal():
    connection = get_db_connection()

    member = connection.execute(
        """
        SELECT *
        FROM members
        WHERE id = ?
        """,
        (session["member_id"],),
    ).fetchone()

    _sync_member_automatic_notifications(
        connection,
        member["id"],
    )
    connection.commit()

    portal_information = (
        _get_member_portal_information(
            connection,
            member,
        )
    )

    digital_rows = connection.execute(
        """
        SELECT
            digital_books.id AS digital_book_id,
            digital_books.read_price,
            digital_books.download_price,
            digital_books.title,
            digital_books.author,
            digital_books.category,
            digital_books.description,
            digital_books.cover_url
        FROM digital_books
        WHERE digital_books.is_active = 1
        ORDER BY digital_books.title
        """
    ).fetchall()

    digital_books = []

    for row in digital_rows:
        item = dict(row)

        item["can_read"] = member_has_digital_access(
            connection,
            member["id"],
            row["digital_book_id"],
            "read",
        )

        item["can_download"] = member_has_digital_access(
            connection,
            member["id"],
            row["digital_book_id"],
            "download",
        )

        digital_books.append(item)

    connection.close()

    return render_template(
        "member_portal.html",
        member=member,
        today_iso=date.today().isoformat(),
        razorpay_key_id=RAZORPAY_KEY_ID,
        digital_books=digital_books,
        **portal_information,
    )


@app.route(
    "/member-portal/report-incident",
    methods=["POST"],
)
@member_card_required
def submit_member_incident():
    member_id = session["member_id"]

    transaction_id_text = request.form.get(
        "transaction_id",
        "",
    ).strip()
    incident_type = request.form.get(
        "incident_type",
        "",
    ).strip()
    noticed_date = request.form.get(
        "noticed_date",
        "",
    ).strip()
    description = request.form.get(
        "description",
        "",
    ).strip()

    try:
        transaction_id = int(
            transaction_id_text
        )
    except (TypeError, ValueError):
        transaction_id = 0

    if incident_type not in {
        "Lost",
        "Damaged",
    }:
        flash(
            "Please select Lost or Damaged.",
            "danger",
        )
        return redirect(
            url_for("member_portal") + "#incidents"
        )

    if len(description) < 10:
        flash(
            "Please provide at least 10 characters "
            "explaining what happened.",
            "danger",
        )
        return redirect(
            url_for("member_portal") + "#incidents"
        )

    if noticed_date:
        try:
            noticed = date.fromisoformat(
                noticed_date
            )
        except ValueError:
            noticed = None

        if (
            noticed is None
            or noticed > date.today()
        ):
            flash(
                "Please enter a valid incident date.",
                "danger",
            )
            return redirect(
                url_for("member_portal")
                + "#incidents"
            )

    connection = get_db_connection()

    transaction = connection.execute(
        """
        SELECT
            transactions.id,
            transactions.book_id,
            books.title
        FROM transactions
        JOIN books
          ON books.id = transactions.book_id
        WHERE transactions.id = ?
          AND transactions.member_id = ?
          AND transactions.status = 'Issued'
        """,
        (
            transaction_id,
            member_id,
        ),
    ).fetchone()

    if transaction is None:
        connection.close()
        flash(
            "That book is not currently issued to your account.",
            "danger",
        )
        return redirect(
            url_for("member_portal") + "#incidents"
        )

    existing_incident = connection.execute(
        """
        SELECT id
        FROM book_incidents
        WHERE transaction_id = ?
          AND status IN (
              'Pending',
              'Approved',
              'Paid',
              'Resolved'
          )
        LIMIT 1
        """,
        (transaction_id,),
    ).fetchone()

    if existing_incident is not None:
        connection.close()
        flash(
            "A Lost or Damaged report already exists "
            "for this issued book.",
            "warning",
        )
        return redirect(
            url_for("member_portal") + "#incidents"
        )

    try:
        cursor = connection.execute(
            """
            INSERT INTO book_incidents (
                transaction_id,
                member_id,
                book_id,
                incident_type,
                description,
                noticed_date,
                charge,
                status,
                reported_at
            )
            VALUES (
                ?, ?, ?, ?, ?, ?,
                0,
                'Pending',
                datetime('now', 'localtime')
            )
            """,
            (
                transaction_id,
                member_id,
                transaction["book_id"],
                incident_type,
                description,
                noticed_date or None,
            ),
        )

        incident_id = cursor.lastrowid

        _create_member_notification(
            connection,
            member_id,
            "Lost or Damaged",
            "Report submitted to the Librarian",
            (
                f"Your {incident_type.lower()} report for "
                f"{transaction['title']} was submitted and "
                "is waiting for Librarian review."
            ),
            unique_key=(
                f"incident:{incident_id}:Pending"
            ),
        )

        connection.commit()

    except psycopg2.Error:
        connection.rollback()
        connection.close()
        flash(
            "The report could not be submitted.",
            "danger",
        )
        return redirect(
            url_for("member_portal") + "#incidents"
        )

    connection.close()

    flash(
        "Report submitted. The Librarian will review it.",
        "success",
    )
    return redirect(
        url_for("member_portal") + "#incidents"
    )


@app.route(
    "/member-card/change-password",
    methods=["GET", "POST"],
)
@member_card_required
def member_change_password():
    connection = get_db_connection()
    member = connection.execute(
        """
        SELECT *
        FROM members
        WHERE id = ?
        """,
        (session["member_id"],),
    ).fetchone()

    if request.method == "POST":
        current_password = request.form.get(
            "current_password",
            "",
        )
        new_password = request.form.get(
            "new_password",
            "",
        )
        confirm_password = request.form.get(
            "confirm_password",
            "",
        )

        if not check_password_hash(
            member["card_password_hash"],
            current_password,
        ):
            connection.close()
            flash(
                "Your current password is incorrect.",
                "danger",
            )
            return redirect(
                url_for(
                    "member_change_password"
                )
            )

        if not _password_is_strong(new_password):
            connection.close()
            flash(
                "Use at least 8 characters with uppercase, "
                "lowercase and one number.",
                "danger",
            )
            return redirect(
                url_for(
                    "member_change_password"
                )
            )

        if current_password == new_password:
            connection.close()
            flash(
                "The new password must be different "
                "from the current password.",
                "warning",
            )
            return redirect(
                url_for(
                    "member_change_password"
                )
            )

        if new_password != confirm_password:
            connection.close()
            flash(
                "The two new passwords do not match.",
                "danger",
            )
            return redirect(
                url_for(
                    "member_change_password"
                )
            )

        connection.execute(
            """
            UPDATE members
            SET card_password_hash = ?,
                card_password_updated_at =
                    datetime('now', 'localtime'),
                card_failed_attempts = 0,
                card_locked_until = NULL,
                card_session_version =
                    card_session_version + 1
            WHERE id = ?
            """,
            (
                generate_password_hash(
                    new_password
                ),
                member["id"],
            ),
        )

        updated_member = connection.execute(
            """
            SELECT *
            FROM members
            WHERE id = ?
            """,
            (member["id"],),
        ).fetchone()

        connection.commit()
        connection.close()

        session["card_session_version"] = (
            updated_member["card_session_version"]
        )
        session["card_last_activity"] = time.time()

        flash(
            "Your Digital Card password was changed successfully.",
            "success",
        )
        return redirect(url_for("member_portal"))

    connection.close()

    return render_template(
        "member_change_password.html",
        member=member,
    )


@app.route(
    "/member/<int:member_id>/reset-card-password",
    methods=["POST"],
)
@login_required
def reset_member_card_password(member_id):
    connection = get_db_connection()
    member = connection.execute(
        """
        SELECT id, name
        FROM members
        WHERE id = ?
        """,
        (member_id,),
    ).fetchone()

    if member is None:
        connection.close()
        flash(
            "Member record was not found.",
            "danger",
        )
        return redirect(url_for("members"))

    connection.execute(
        """
        UPDATE members
        SET card_password_hash = NULL,
            card_password_updated_at =
                datetime('now', 'localtime'),
            card_failed_attempts = 0,
            card_locked_until = NULL,
            card_reset_required = 1,
            card_session_version =
                card_session_version + 1
        WHERE id = ?
        """,
        (member_id,),
    )

    connection.commit()
    connection.close()

    flash(
        f"Password reset for {member['name']}. "
        "The student must create a new password after scanning.",
        "success",
    )
    return redirect(
        url_for(
            "member_profile",
            member_id=member_id,
        )
    )


@app.route(
    "/member/<int:member_id>/unlock-card",
    methods=["POST"],
)
@login_required
def unlock_member_card(member_id):
    connection = get_db_connection()

    result = connection.execute(
        """
        UPDATE members
        SET card_failed_attempts = 0,
            card_locked_until = NULL
        WHERE id = ?
        """,
        (member_id,),
    )

    connection.commit()
    connection.close()

    if result.rowcount != 1:
        flash(
            "Member record was not found.",
            "danger",
        )
        return redirect(url_for("members"))

    flash(
        "The student Digital Card was unlocked.",
        "success",
    )
    return redirect(
        url_for(
            "member_profile",
            member_id=member_id,
        )
    )


# -------------------------------------------------
# STUDENT NOTIFICATIONS
# -------------------------------------------------

@app.route(
    "/member-portal/notification/"
    "<int:notification_id>/read",
    methods=["POST"],
)
@member_card_required
def mark_member_notification_read(
    notification_id,
):
    member_id = session["member_id"]
    connection = get_db_connection()

    notification = connection.execute(
        """
        SELECT id
        FROM notifications
        WHERE id = ?
          AND is_active = 1
          AND (
                member_id = ?
                OR recipient_type = 'all'
              )
        """,
        (
            notification_id,
            member_id,
        ),
    ).fetchone()

    if notification is not None:
        connection.execute(
            """
            INSERT OR IGNORE INTO notification_reads (
                notification_id,
                member_id,
                read_at
            )
            VALUES (
                ?, ?,
                datetime('now', 'localtime')
            )
            """,
            (
                notification_id,
                member_id,
            ),
        )
        connection.commit()

    connection.close()

    return redirect(
        url_for("member_portal")
        + "#notifications"
    )


@app.route(
    "/member-portal/notifications/read-all",
    methods=["POST"],
)
@member_card_required
def mark_all_member_notifications_read():
    member_id = session["member_id"]
    connection = get_db_connection()

    notification_rows = connection.execute(
        """
        SELECT id
        FROM notifications
        WHERE is_active = 1
          AND (
                member_id = ?
                OR recipient_type = 'all'
              )
          AND (
                expires_at IS NULL
                OR TRIM(expires_at) = ''
                OR date(expires_at)
                   >= date('now', 'localtime')
              )
        """,
        (member_id,),
    ).fetchall()

    connection.executemany(
        """
        INSERT OR IGNORE INTO notification_reads (
            notification_id,
            member_id,
            read_at
        )
        VALUES (
            ?, ?,
            datetime('now', 'localtime')
        )
        """,
        [
            (
                row["id"],
                member_id,
            )
            for row in notification_rows
        ],
    )

    connection.commit()
    connection.close()

    flash(
        "All notifications were marked as read.",
        "success",
    )
    return redirect(
        url_for("member_portal")
        + "#notifications"
    )


@app.route(
    "/librarian/notifications",
    methods=["GET", "POST"],
)
@login_required
def librarian_notifications():
    connection = get_db_connection()

    if request.method == "POST":
        recipient_type = request.form.get("recipient_type", "member").strip()
        member_id_text = request.form.get("member_id", "").strip()
        category = request.form.get("category", "General").strip()
        priority = request.form.get("priority", "Normal").strip()
        title = request.form.get("title", "").strip()
        message = request.form.get("message", "").strip()
        expires_at = request.form.get("expires_at", "").strip()
        send_mode = request.form.get("send_mode", "portal").strip()

        if recipient_type not in {"member", "all"}:
            recipient_type = "member"
        if category not in {
            "General", "Book Issue", "Book Return", "Due Date", "Overdue",
            "Penalty", "Lost or Damaged", "Payment", "Library Announcement",
        }:
            category = "General"
        if priority not in {"Normal", "Important", "Urgent"}:
            priority = "Normal"
        if send_mode not in {"portal", "portal_whatsapp"}:
            send_mode = "portal"

        if len(title) < 3 or len(message) < 5:
            connection.close()
            flash("Enter a valid title and message.", "danger")
            return redirect(url_for("librarian_notifications"))

        member_id = None
        selected_member = None

        if recipient_type == "member":
            try:
                member_id = int(member_id_text)
            except (TypeError, ValueError):
                member_id = 0

            selected_member = connection.execute(
                """
                SELECT id, name, phone, membership_id
                FROM members
                WHERE id = ?
                """,
                (member_id,),
            ).fetchone()

            if selected_member is None:
                connection.close()
                flash("Please select a valid member.", "danger")
                return redirect(url_for("librarian_notifications"))

        if send_mode == "portal_whatsapp" and recipient_type != "member":
            connection.close()
            flash(
                "Manual WhatsApp sending works for one selected student at a time.",
                "warning",
            )
            return redirect(url_for("librarian_notifications"))

        if expires_at:
            try:
                expiry_date = date.fromisoformat(expires_at)
            except ValueError:
                expiry_date = None
            if expiry_date is None or expiry_date < date.today():
                connection.close()
                flash("Expiry date cannot be in the past.", "danger")
                return redirect(url_for("librarian_notifications"))

        connection.execute(
            """
            INSERT INTO notifications (
                member_id, recipient_type, category, priority, title, message,
                created_at, expires_at, created_by, is_active
            )
            VALUES (?, ?, ?, ?, ?, ?, datetime('now', 'localtime'), ?, ?, 1)
            """,
            (
                member_id, recipient_type, category, priority, title, message,
                expires_at or None, session.get("username", "Librarian"),
            ),
        )
        connection.commit()

        if send_mode == "portal_whatsapp" and selected_member is not None:
            whatsapp_phone = _prepare_whatsapp_phone(selected_member["phone"])
            if not whatsapp_phone:
                connection.close()
                flash(
                    "Notification was saved in the Student Portal, but the student phone number is invalid.",
                    "warning",
                )
                return redirect(url_for("librarian_notifications"))

            whatsapp_message = (
                "📚 *RSIET Library*\n\n"
                f"Hello {selected_member['name']},\n\n"
                f"*{title}*\n\n"
                f"{message}\n\n"
                "— RSIET Library\n"
                "Aureon Digital Library"
            )
            whatsapp_url = (
                f"https://wa.me/{whatsapp_phone}?text="
                f"{quote(whatsapp_message, safe='')}"
            )
            connection.close()
            return redirect(whatsapp_url)

        connection.close()
        flash("Notification sent successfully to the Student Portal.", "success")
        return redirect(url_for("librarian_notifications"))

    search = request.args.get(
        "search",
        "",
    ).strip()
    selected_category = request.args.get(
        "category",
        "",
    ).strip()

    members_list = connection.execute(
        """
        SELECT
            id,
            membership_id,
            name,
            department
        FROM members
        ORDER BY name
        """
    ).fetchall()

    query = """
        SELECT
            notifications.*,
            members.name AS member_name,
            members.membership_id
        FROM notifications
        LEFT JOIN members
          ON members.id = notifications.member_id
        WHERE 1 = 1
    """
    values = []

    if search:
        search_value = f"%{search}%"
        query += """
            AND (
                notifications.title LIKE ?
                OR notifications.message LIKE ?
                OR notifications.category LIKE ?
                OR members.name LIKE ?
                OR members.membership_id LIKE ?
            )
        """
        values.extend([
            search_value,
            search_value,
            search_value,
            search_value,
            search_value,
        ])

    if selected_category:
        query += """
            AND notifications.category = ?
        """
        values.append(selected_category)

    query += """
        ORDER BY notifications.id DESC
    """

    notification_rows = connection.execute(
        query,
        values,
    ).fetchall()

    total_members = connection.execute(
        """
        SELECT COUNT(*)
        FROM members
        """
    ).fetchone()[0]

    sent_notifications = []
    members_reached = set()
    unread_total = 0
    active_messages = 0

    for row in notification_rows:
        item = dict(row)

        if row["recipient_type"] == "all":
            delivered_count = total_members
        else:
            delivered_count = 1
            if row["member_id"]:
                members_reached.add(
                    row["member_id"]
                )

        read_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM notification_reads
            WHERE notification_id = ?
            """,
            (row["id"],),
        ).fetchone()[0]

        unread_count = max(
            delivered_count - read_count,
            0,
        )

        expired = False

        if row["expires_at"]:
            try:
                expired = (
                    date.fromisoformat(
                        row["expires_at"]
                    )
                    < date.today()
                )
            except ValueError:
                expired = False

        item["notification_id"] = row["id"]
        item["delivered_count"] = delivered_count
        item["read_count"] = read_count
        item["unread_count"] = unread_count
        item["is_expired"] = expired

        if row["is_active"] and not expired:
            active_messages += 1
            unread_total += unread_count

        sent_notifications.append(item)

    all_recipients_exist = any(
        item["recipient_type"] == "all"
        for item in sent_notifications
    )

    if all_recipients_exist:
        members_reached_count = total_members
    else:
        members_reached_count = len(
            members_reached
        )

    notification_summary = {
        "total_sent": len(sent_notifications),
        "active_messages": active_messages,
        "unread_messages": unread_total,
        "members_reached":
            members_reached_count,
    }

    notification_categories = [
        "General",
        "Book Issue",
        "Book Return",
        "Due Date",
        "Overdue",
        "Penalty",
        "Lost or Damaged",
        "Payment",
        "Library Announcement",
    ]

    connection.close()

    return render_template(
        "librarian_notifications.html",
        members=members_list,
        notification_summary=notification_summary,
        sent_notifications=sent_notifications,
        notification_categories=
            notification_categories,
        selected_category=selected_category,
        search=search,
        today_iso=date.today().isoformat(),
    )




@app.route(
    "/librarian/notifications/"
    "<int:notification_id>/delete",
    methods=["POST"],
)
@login_required
def delete_librarian_notification(
    notification_id,
):
    connection = get_db_connection()

    connection.execute(
        """
        DELETE FROM notification_reads
        WHERE notification_id = ?
        """,
        (notification_id,),
    )

    result = connection.execute(
        """
        DELETE FROM notifications
        WHERE id = ?
        """,
        (notification_id,),
    )

    connection.commit()
    connection.close()

    flash(
        (
            "Notification deleted successfully."
            if result.rowcount == 1
            else "Notification was not found."
        ),
        (
            "success"
            if result.rowcount == 1
            else "warning"
        ),
    )

    return redirect(
        url_for("librarian_notifications")
    )


# -------------------------------------------------
# STUDENT FINE PAYMENTS
# -------------------------------------------------

@app.route(
    "/member-portal/fine/"
    "<int:transaction_id>/request-cash",
    methods=["POST"],
)
@member_card_required
def request_fine_cash(transaction_id):
    member_id = session["member_id"]
    connection = get_db_connection()

    transaction = _find_payable_transaction(
        connection,
        member_id,
        transaction_id,
    )

    if (
        transaction is None
        or transaction["fine"] <= 0
    ):
        connection.close()
        flash(
            "That fine is not available for payment.",
            "danger",
        )
        return redirect(
            url_for("member_portal")
            + "#payments"
        )

    paid = connection.execute(
        """
        SELECT id
        FROM fine_payments
        WHERE transaction_id = ?
        """,
        (transaction_id,),
    ).fetchone()

    if paid is not None:
        connection.close()
        flash(
            "This fine has already been paid.",
            "warning",
        )
        return redirect(
            url_for("member_portal")
            + "#payments"
        )

    try:
        connection.execute(
            """
            INSERT INTO fine_payment_requests (
                transaction_id,
                member_id,
                amount,
                payment_method,
                payment_status,
                cash_requested_at,
                created_at
            )
            VALUES (
                ?, ?, ?, 'Cash',
                'Awaiting Cash Confirmation',
                datetime('now', 'localtime'),
                datetime('now', 'localtime')
            )
            ON CONFLICT(transaction_id)
            DO UPDATE SET
                member_id = excluded.member_id,
                amount = excluded.amount,
                payment_method = 'Cash',
                payment_status =
                    'Awaiting Cash Confirmation',
                payment_reference = NULL,
                gateway_order_id = NULL,
                gateway_payment_id = NULL,
                gateway_signature = NULL,
                cash_requested_at =
                    datetime('now', 'localtime'),
                cash_confirmed_by = NULL,
                paid_at = NULL
            """,
            (
                transaction_id,
                member_id,
                transaction["fine"],
            ),
        )

        _create_member_notification(
            connection,
            member_id,
            "Payment",
            "Cash fine payment requested",
            (
                f"Your Cash payment request for the "
                f"₹{transaction['fine']} late-return fine "
                "was submitted. Pay the Librarian."
            ),
            priority="Important",
            unique_key=(
                f"fine:{transaction_id}:cash-request:"
                f"{date.today().isoformat()}"
            ),
        )

        connection.commit()

    except psycopg2.Error:
        connection.rollback()
        connection.close()
        flash(
            "Cash payment request could not be saved.",
            "danger",
        )
        return redirect(
            url_for("member_portal")
            + "#payments"
        )

    connection.close()

    flash(
        "Cash request submitted. "
        "Pay the amount to the Librarian.",
        "success",
    )
    return redirect(
        url_for("member_portal")
        + "#payments"
    )


@app.route(
    "/librarian/fine-payment-request/"
    "<int:request_id>/confirm-cash",
    methods=["POST"],
)
@login_required
def confirm_fine_cash(request_id):
    connection = get_db_connection()

    payment_request = connection.execute(
        """
        SELECT
            fine_payment_requests.*,
            transactions.fine,
            books.title
        FROM fine_payment_requests
        JOIN transactions
          ON transactions.id =
             fine_payment_requests.transaction_id
        JOIN books
          ON books.id = transactions.book_id
        WHERE fine_payment_requests.id = ?
          AND fine_payment_requests.payment_method = 'Cash'
          AND fine_payment_requests.payment_status =
              'Awaiting Cash Confirmation'
        """,
        (request_id,),
    ).fetchone()

    if payment_request is None:
        connection.close()
        flash(
            "Cash fine request was not found.",
            "danger",
        )
        return redirect(url_for("overdue"))

    transaction = _find_payable_transaction(
        connection,
        payment_request["member_id"],
        payment_request["transaction_id"],
    )

    if transaction is None:
        connection.close()
        flash(
            "The fine transaction was not found.",
            "danger",
        )
        return redirect(url_for("overdue"))

    confirmed_by = session.get(
        "username",
        "Librarian",
    )

    try:
        payment_id, payment_error = (
            _create_fine_payment(
                connection,
                transaction,
                "Cash",
                f"Cash received by {confirmed_by}",
            )
        )

        if payment_error == "already_paid":
            connection.execute(
                """
                UPDATE fine_payment_requests
                SET payment_status = 'Paid'
                WHERE id = ?
                """,
                (request_id,),
            )
        else:
            connection.execute(
                """
                UPDATE fine_payment_requests
                SET payment_status = 'Paid',
                    payment_reference = ?,
                    cash_confirmed_by = ?,
                    paid_at =
                        datetime('now', 'localtime')
                WHERE id = ?
                """,
                (
                    f"Cash received by {confirmed_by}",
                    confirmed_by,
                    request_id,
                ),
            )

        _create_member_notification(
            connection,
            payment_request["member_id"],
            "Payment",
            "Cash fine payment confirmed",
            (
                f"Your Cash payment of "
                f"₹{payment_request['fine']} for "
                f"{payment_request['title']} was "
                "confirmed by the Librarian."
            ),
            unique_key=(
                f"fine:{payment_request['transaction_id']}:paid"
            ),
        )

        connection.commit()
        _send_member_payment_whatsapp(
            connection,
            payment_request["member_id"],
            payment_request["fine"],
            f"Fine payment for {payment_request['title']}",
            f"fine-payment:{payment_request['transaction_id']}:paid",
        )

    except psycopg2.Error:
        connection.rollback()
        connection.close()
        flash(
            "Cash payment could not be confirmed.",
            "danger",
        )
        return redirect(url_for("overdue"))

    connection.close()

    flash(
        "Cash fine payment confirmed successfully.",
        "success",
    )
    return redirect(url_for("overdue"))


@app.route(
    "/member-portal/fine/"
    "<int:transaction_id>/pay-online",
    methods=["POST"],
)
@member_card_required
def start_fine_payment(transaction_id):
    member_id = session["member_id"]
    connection = get_db_connection()

    transaction = _find_payable_transaction(
        connection,
        member_id,
        transaction_id,
    )

    if (
        transaction is None
        or transaction["fine"] <= 0
    ):
        connection.close()
        flash(
            "That fine is not available for payment.",
            "danger",
        )
        return redirect(
            url_for("member_portal")
            + "#payments"
        )

    existing_payment = connection.execute(
        """
        SELECT id
        FROM fine_payments
        WHERE transaction_id = ?
        """,
        (transaction_id,),
    ).fetchone()

    if existing_payment is not None:
        connection.close()
        flash(
            "This fine has already been paid.",
            "warning",
        )
        return redirect(
            url_for("member_portal")
            + "#payments"
        )

    member = connection.execute(
        """
        SELECT *
        FROM members
        WHERE id = ?
        """,
        (member_id,),
    ).fetchone()

    book = connection.execute(
        """
        SELECT books.title
        FROM transactions
        JOIN books
          ON books.id = transactions.book_id
        WHERE transactions.id = ?
        """,
        (transaction_id,),
    ).fetchone()

    client = get_razorpay_client()

    if client is None:
        connection.close()
        flash(
            "Online payment is not configured. "
            "Please use Cash payment or contact the Librarian.",
            "warning",
        )
        return redirect(
            url_for("member_portal")
            + "#payments"
        )

    amount_in_paise = int(
        transaction["fine"]
    ) * 100

    try:
        order = client.order.create({
            "amount": amount_in_paise,
            "currency": "INR",
            "receipt": (
                f"fine_{transaction_id}_"
                f"{secrets.token_hex(4)}"
            ),
            "notes": {
                "transaction_id":
                    str(transaction_id),
                "member_id": str(member_id),
            },
        })

        connection.execute(
            """
            INSERT INTO fine_payment_requests (
                transaction_id,
                member_id,
                amount,
                payment_method,
                payment_status,
                gateway_order_id,
                created_at
            )
            VALUES (
                ?, ?, ?, 'Online',
                'Pending', ?,
                datetime('now', 'localtime')
            )
            ON CONFLICT(transaction_id)
            DO UPDATE SET
                member_id = excluded.member_id,
                amount = excluded.amount,
                payment_method = 'Online',
                payment_status = 'Pending',
                payment_reference = NULL,
                gateway_order_id =
                    excluded.gateway_order_id,
                gateway_payment_id = NULL,
                gateway_signature = NULL,
                cash_requested_at = NULL,
                cash_confirmed_by = NULL,
                paid_at = NULL
            """,
            (
                transaction_id,
                member_id,
                transaction["fine"],
                order["id"],
            ),
        )

        connection.commit()

    except Exception as error:
        connection.rollback()
        connection.close()
        print(
            "Fine Razorpay order error:",
            error,
        )
        flash(
            "The online payment order could not be created.",
            "danger",
        )
        return redirect(
            url_for("member_portal")
            + "#payments"
        )

    connection.close()

    return render_template(
        "member_payment_checkout.html",
        payment_kind="fine",
        page_title="Pay Book Fine",
        description=book["title"],
        amount=transaction["fine"],
        amount_in_paise=amount_in_paise,
        order_id=order["id"],
        key_id=RAZORPAY_KEY_ID,
        member=member,
        verify_url=url_for(
            "verify_fine_payment",
            transaction_id=transaction_id,
        ),
        success_url=(
            url_for("member_portal")
            + "#payments"
        ),
    )


@app.route(
    "/member-portal/fine/"
    "<int:transaction_id>/verify",
    methods=["POST"],
)
@member_card_required
def verify_fine_payment(transaction_id):
    member_id = session["member_id"]
    data = request.get_json(
        silent=True,
    ) or {}

    order_id = str(
        data.get("razorpay_order_id", "")
    ).strip()
    payment_id = str(
        data.get("razorpay_payment_id", "")
    ).strip()
    signature = str(
        data.get("razorpay_signature", "")
    ).strip()

    if not all((
        order_id,
        payment_id,
        signature,
    )):
        return jsonify({
            "success": False,
            "message": "Incomplete payment information.",
        }), 400

    connection = get_db_connection()

    payment_request = connection.execute(
        """
        SELECT *
        FROM fine_payment_requests
        WHERE transaction_id = ?
          AND member_id = ?
          AND gateway_order_id = ?
        """,
        (
            transaction_id,
            member_id,
            order_id,
        ),
    ).fetchone()

    transaction = _find_payable_transaction(
        connection,
        member_id,
        transaction_id,
    )

    if (
        payment_request is None
        or transaction is None
    ):
        connection.close()
        return jsonify({
            "success": False,
            "message": "Payment record was not found.",
        }), 404

    client = get_razorpay_client()

    if client is None:
        connection.close()
        return jsonify({
            "success": False,
            "message": "Payment service is unavailable.",
        }), 503

    expected_amount = int(
        transaction["fine"]
    ) * 100

    try:
        client.utility.verify_payment_signature({
            "razorpay_order_id": order_id,
            "razorpay_payment_id": payment_id,
            "razorpay_signature": signature,
        })

        gateway_payment = client.payment.fetch(
            payment_id
        )

        if (
            gateway_payment.get("status")
            == "authorized"
        ):
            client.payment.capture(
                payment_id,
                expected_amount,
            )
            gateway_payment = (
                client.payment.fetch(
                    payment_id
                )
            )

        if (
            gateway_payment.get("status")
            != "captured"
            or int(
                gateway_payment.get(
                    "amount",
                    0,
                )
            ) != expected_amount
            or gateway_payment.get("currency")
            != "INR"
        ):
            raise ValueError(
                "Gateway amount or status mismatch."
            )

        final_payment_id, payment_error = (
            _create_fine_payment(
                connection,
                transaction,
                "Online",
                payment_id,
            )
        )

        if payment_error == "already_paid":
            final_payment = connection.execute(
                """
                SELECT id
                FROM fine_payments
                WHERE transaction_id = ?
                """,
                (transaction_id,),
            ).fetchone()
            final_payment_id = final_payment["id"]

        connection.execute(
            """
            UPDATE fine_payment_requests
            SET payment_status = 'Paid',
                payment_reference = ?,
                gateway_payment_id = ?,
                gateway_signature = ?,
                paid_at =
                    datetime('now', 'localtime')
            WHERE id = ?
            """,
            (
                payment_id,
                payment_id,
                signature,
                payment_request["id"],
            ),
        )

        _create_member_notification(
            connection,
            member_id,
            "Payment",
            "Online fine payment completed",
            (
                f"Your online payment of "
                f"₹{transaction['fine']} was completed."
            ),
            unique_key=(
                f"fine:{transaction_id}:paid"
            ),
        )

        connection.commit()
        _send_member_payment_whatsapp(
            connection,
            member_id,
            transaction["fine"],
            "Online late-return fine payment",
            f"fine-payment:{transaction_id}:paid",
        )

    except Exception as error:
        connection.rollback()
        connection.close()
        print(
            "Fine payment verification error:",
            error,
        )
        return jsonify({
            "success": False,
            "message": "Payment verification failed.",
        }), 400

    connection.close()

    return jsonify({
        "success": True,
        "receipt_url": url_for(
            "member_fine_receipt",
            transaction_id=transaction_id,
        ),
    })


# -------------------------------------------------
# STUDENT INCIDENT ONLINE PAYMENT
# -------------------------------------------------

@app.route(
    "/member-portal/incident/"
    "<int:incident_id>/pay-online",
    methods=["POST"],
)
@member_card_required
def start_incident_payment(incident_id):
    member_id = session["member_id"]
    connection = get_db_connection()

    incident = connection.execute(
        """
        SELECT
            book_incidents.*,
            books.title
        FROM book_incidents
        JOIN books
          ON books.id = book_incidents.book_id
        WHERE book_incidents.id = ?
          AND book_incidents.member_id = ?
          AND book_incidents.status = 'Approved'
          AND book_incidents.charge > 0
        """,
        (
            incident_id,
            member_id,
        ),
    ).fetchone()

    if incident is None:
        connection.close()
        flash(
            "That incident charge is not available for payment.",
            "danger",
        )
        return redirect(
            url_for("member_portal")
            + "#payments"
        )

    existing_payment = connection.execute(
        """
        SELECT *
        FROM incident_payments
        WHERE incident_id = ?
        """,
        (incident_id,),
    ).fetchone()

    if (
        existing_payment is not None
        and existing_payment["payment_status"] == "Paid"
    ):
        connection.close()
        flash(
            "This charge has already been paid.",
            "warning",
        )
        return redirect(
            url_for("member_portal")
            + "#payments"
        )

    member = connection.execute(
        """
        SELECT *
        FROM members
        WHERE id = ?
        """,
        (member_id,),
    ).fetchone()

    client = get_razorpay_client()

    if client is None:
        connection.close()
        flash(
            "Online payment is not configured. "
            "Please use Cash payment or contact the Librarian.",
            "warning",
        )
        return redirect(
            url_for("member_portal")
            + "#payments"
        )

    amount_in_paise = int(
        incident["charge"]
    ) * 100

    try:
        order = client.order.create({
            "amount": amount_in_paise,
            "currency": "INR",
            "receipt": (
                f"incident_{incident_id}_"
                f"{secrets.token_hex(4)}"
            ),
            "notes": {
                "incident_id": str(incident_id),
                "member_id": str(member_id),
            },
        })

        if existing_payment is None:
            connection.execute(
                """
                INSERT INTO incident_payments (
                    incident_id,
                    amount,
                    payment_method,
                    payment_status,
                    gateway_order_id
                )
                VALUES (
                    ?, ?, 'Online',
                    'Pending', ?
                )
                """,
                (
                    incident_id,
                    incident["charge"],
                    order["id"],
                ),
            )
        else:
            connection.execute(
                """
                UPDATE incident_payments
                SET amount = ?,
                    payment_method = 'Online',
                    payment_status = 'Pending',
                    payment_reference = NULL,
                    gateway_order_id = ?,
                    gateway_payment_id = NULL,
                    gateway_signature = NULL,
                    cash_requested_at = NULL,
                    cash_confirmed_by = NULL,
                    paid_at = NULL,
                    receipt_no = NULL
                WHERE incident_id = ?
                """,
                (
                    incident["charge"],
                    order["id"],
                    incident_id,
                ),
            )

        connection.commit()

    except Exception as error:
        connection.rollback()
        connection.close()
        print(
            "Incident Razorpay order error:",
            error,
        )
        flash(
            "The online payment order could not be created.",
            "danger",
        )
        return redirect(
            url_for("member_portal")
            + "#payments"
        )

    connection.close()

    return render_template(
        "member_payment_checkout.html",
        payment_kind="incident",
        page_title="Pay Lost or Damaged Charge",
        description=(
            f"{incident['title']} "
            f"({incident['incident_type']})"
        ),
        amount=incident["charge"],
        amount_in_paise=amount_in_paise,
        order_id=order["id"],
        key_id=RAZORPAY_KEY_ID,
        member=member,
        verify_url=url_for(
            "verify_member_incident_payment",
            incident_id=incident_id,
        ),
        success_url=(
            url_for("member_portal")
            + "#payments"
        ),
    )


@app.route(
    "/member-portal/incident/"
    "<int:incident_id>/verify",
    methods=["POST"],
)
@member_card_required
def verify_member_incident_payment(
    incident_id,
):
    member_id = session["member_id"]
    data = request.get_json(
        silent=True,
    ) or {}

    order_id = str(
        data.get("razorpay_order_id", "")
    ).strip()
    payment_id = str(
        data.get("razorpay_payment_id", "")
    ).strip()
    signature = str(
        data.get("razorpay_signature", "")
    ).strip()

    if not all((
        order_id,
        payment_id,
        signature,
    )):
        return jsonify({
            "success": False,
            "message": "Incomplete payment information.",
        }), 400

    connection = get_db_connection()

    payment = connection.execute(
        """
        SELECT
            incident_payments.*,
            book_incidents.member_id,
            book_incidents.charge
        FROM incident_payments
        JOIN book_incidents
          ON book_incidents.id =
             incident_payments.incident_id
        WHERE incident_payments.incident_id = ?
          AND book_incidents.member_id = ?
          AND incident_payments.gateway_order_id = ?
        """,
        (
            incident_id,
            member_id,
            order_id,
        ),
    ).fetchone()

    if payment is None:
        connection.close()
        return jsonify({
            "success": False,
            "message": "Payment record was not found.",
        }), 404

    client = get_razorpay_client()

    if client is None:
        connection.close()
        return jsonify({
            "success": False,
            "message": "Payment service is unavailable.",
        }), 503

    expected_amount = int(
        payment["charge"]
    ) * 100

    try:
        client.utility.verify_payment_signature({
            "razorpay_order_id": order_id,
            "razorpay_payment_id": payment_id,
            "razorpay_signature": signature,
        })

        gateway_payment = client.payment.fetch(
            payment_id
        )

        if (
            gateway_payment.get("status")
            == "authorized"
        ):
            client.payment.capture(
                payment_id,
                expected_amount,
            )
            gateway_payment = (
                client.payment.fetch(
                    payment_id
                )
            )

        if (
            gateway_payment.get("status")
            != "captured"
            or int(
                gateway_payment.get(
                    "amount",
                    0,
                )
            ) != expected_amount
            or gateway_payment.get("currency")
            != "INR"
        ):
            raise ValueError(
                "Gateway amount or status mismatch."
            )

        receipt_no = f"AUR-I-{payment['id']:05d}"

        connection.execute(
            """
            UPDATE incident_payments
            SET receipt_no = ?,
                payment_status = 'Paid',
                payment_reference = ?,
                gateway_payment_id = ?,
                gateway_signature = ?,
                paid_at =
                    datetime('now', 'localtime')
            WHERE id = ?
            """,
            (
                receipt_no,
                payment_id,
                payment_id,
                signature,
                payment["id"],
            ),
        )

        connection.execute(
            """
            UPDATE book_incidents
            SET status = 'Paid'
            WHERE id = ?
            """,
            (incident_id,),
        )

        _create_member_notification(
            connection,
            member_id,
            "Payment",
            "Incident payment completed",
            (
                f"Your online incident payment of "
                f"₹{payment['charge']} was completed."
            ),
            unique_key=(
                f"incident:{incident_id}:Paid"
            ),
        )

        connection.commit()
        _send_member_payment_whatsapp(
            connection,
            member_id,
            payment["charge"],
            "Online lost or damaged book charge",
            f"incident-payment:{incident_id}:paid",
        )
        _send_incident_whatsapp(
            connection,
            incident_id,
            "Paid",
        )

    except Exception as error:
        connection.rollback()
        connection.close()
        print(
            "Incident payment verification error:",
            error,
        )
        return jsonify({
            "success": False,
            "message": "Payment verification failed.",
        }), 400

    connection.close()

    return jsonify({
        "success": True,
        "receipt_url": url_for(
            "member_incident_receipt",
            incident_id=incident_id,
        ),
    })


# -------------------------------------------------
# STUDENT RECEIPT ACCESS
# -------------------------------------------------

@app.route(
    "/member-portal/fine-receipt/"
    "<int:transaction_id>"
)
@member_card_required
def member_fine_receipt(transaction_id):
    connection = get_db_connection()

    payment = connection.execute(
        """
        SELECT fine_payments.id
        FROM fine_payments
        JOIN transactions
          ON transactions.id =
             fine_payments.transaction_id
        WHERE fine_payments.transaction_id = ?
          AND transactions.member_id = ?
        """,
        (
            transaction_id,
            session["member_id"],
        ),
    ).fetchone()

    if payment is None:
        connection.close()
        return "Receipt was not found.", 404

    receipt = get_fine_receipt(
        connection,
        payment["id"],
    )
    connection.close()

    return render_template(
        "fine_receipt.html",
        receipt=receipt,
        qr_view=True,
    )


@app.route(
    "/member-portal/incident-receipt/"
    "<int:incident_id>"
)
@member_card_required
def member_incident_receipt(incident_id):
    connection = get_db_connection()

    payment = connection.execute(
        """
        SELECT incident_payments.id
        FROM incident_payments
        JOIN book_incidents
          ON book_incidents.id =
             incident_payments.incident_id
        WHERE incident_payments.incident_id = ?
          AND book_incidents.member_id = ?
          AND incident_payments.payment_status = 'Paid'
        """,
        (
            incident_id,
            session["member_id"],
        ),
    ).fetchone()

    if payment is None:
        connection.close()
        return "Receipt was not found.", 404

    receipt = get_incident_receipt(
        connection,
        payment["id"],
    )
    connection.close()

    return render_template(
        "incident_receipt.html",
        receipt=receipt,
        qr_token=session.get(
            "member_qr_token"
        ),
    )


# -------------------------------------------------
# AUREON CHATBOT
# -------------------------------------------------

@app.route(
    "/api/aureon-chat",
    methods=["POST"],
)
@member_card_required
def aureon_chat():
    data = request.get_json(silent=True) or {}
    question = data.get("question", "")

    connection = get_db_connection()
    try:
        result = answer_student_question(
            connection,
            session["member_id"],
            question,
        )
    finally:
        connection.close()

    return jsonify({
        "success": True,
        **result,
    })


# -------------------------------------------------
# LIBRARIAN DIGITAL-BOOK MANAGEMENT
# -------------------------------------------------

@app.route(
    "/books/<int:book_id>/digital",
    methods=["POST"],
)
@login_required
def save_digital_book(book_id):
    connection = get_db_connection()
    book = connection.execute(
        """
        SELECT id, title
        FROM books
        WHERE id = ?
        """,
        (book_id,),
    ).fetchone()

    if book is None:
        connection.close()
        flash("Book record was not found.", "danger")
        return redirect(url_for("books"))

    try:
        read_price = int(
            request.form.get("read_price", "10")
        )
        download_price = int(
            request.form.get("download_price", "100")
        )
    except (TypeError, ValueError):
        connection.close()
        flash("Enter valid digital-book prices.", "danger")
        return redirect(url_for("books"))

    if read_price not in {10, 20}:
        connection.close()
        flash("Reading price must be ₹10 or ₹20.", "danger")
        return redirect(url_for("books"))

    if download_price != 100:
        connection.close()
        flash("Download price must be ₹100.", "danger")
        return redirect(url_for("books"))

    existing = connection.execute(
        """
        SELECT *
        FROM digital_books
        WHERE book_id = ?
        """,
        (book_id,),
    ).fetchone()

    pdf_file = request.files.get("pdf_file")
    has_new_pdf = bool(
        pdf_file
        and str(pdf_file.filename or "").strip()
    )

    if existing is None and not has_new_pdf:
        connection.close()
        flash("Select a PDF file for the digital book.", "danger")
        return redirect(url_for("books"))

    old_filename = (
        existing["pdf_filename"]
        if existing is not None
        else None
    )
    stored_filename = old_filename
    new_filename = None

    try:
        if has_new_pdf:
            new_filename = save_private_pdf(
                pdf_file,
                book_id,
            )
            stored_filename = new_filename

        connection.execute(
            """
            INSERT INTO digital_books (
                book_id,
                pdf_filename,
                read_price,
                download_price,
                is_active,
                uploaded_at,
                updated_at
            )
            VALUES (
                ?, ?, ?, ?, 1,
                datetime('now', 'localtime'),
                datetime('now', 'localtime')
            )
            ON CONFLICT(book_id)
            DO UPDATE SET
                pdf_filename = excluded.pdf_filename,
                read_price = excluded.read_price,
                download_price = excluded.download_price,
                is_active = 1,
                updated_at = datetime('now', 'localtime')
            """,
            (
                book_id,
                stored_filename,
                read_price,
                download_price,
            ),
        )
        connection.commit()

        if (
            new_filename
            and old_filename
            and old_filename != new_filename
        ):
            delete_private_pdf(old_filename)

    except (ValueError, OSError, psycopg2.Error) as error:
        connection.rollback()

        if new_filename:
            delete_private_pdf(new_filename)

        connection.close()
        flash(str(error), "danger")
        return redirect(url_for("books"))

    connection.close()
    flash(
        f"Digital access updated for {book['title']}.",
        "success",
    )
    return redirect(url_for("books"))


@app.route(
    "/books/<int:book_id>/digital/delete",
    methods=["POST"],
)
@login_required
def delete_digital_book(book_id):
    connection = get_db_connection()
    digital_book = connection.execute(
        """
        SELECT *
        FROM digital_books
        WHERE book_id = ?
        """,
        (book_id,),
    ).fetchone()

    if digital_book is None:
        connection.close()
        flash("Digital-book record was not found.", "warning")
        return redirect(url_for("books"))

    paid_access = connection.execute(
        """
        SELECT id
        FROM digital_book_payments
        WHERE digital_book_id = ?
          AND payment_status = 'Paid'
        LIMIT 1
        """,
        (digital_book["id"],),
    ).fetchone()

    if paid_access is not None:
        connection.execute(
            """
            UPDATE digital_books
            SET is_active = 0,
                updated_at = datetime('now', 'localtime')
            WHERE id = ?
            """,
            (digital_book["id"],),
        )
        message = (
            "Digital book was unpublished. Existing paid access remains valid."
        )
    else:
        delete_private_pdf(digital_book["pdf_filename"])
        connection.execute(
            """
            DELETE FROM digital_books
            WHERE id = ?
            """,
            (digital_book["id"],),
        )
        message = "Digital book was removed successfully."

    connection.commit()
    connection.close()
    flash(message, "success")
    return redirect(url_for("books"))


# -------------------------------------------------
# STUDENT PAID DIGITAL BOOKS
# -------------------------------------------------


def _get_digital_book_record(
    connection,
    digital_book_id,
    require_active=True,
):
    query = """
        SELECT
            digital_books.*,
            books.title,
            books.author,
            books.cover_url,
            books.category,
            books.description
        FROM digital_books
        JOIN books
          ON books.id = digital_books.book_id
        WHERE digital_books.id = ?
    """

    if require_active:
        query += " AND digital_books.is_active = 1"

    return connection.execute(
        query,
        (digital_book_id,),
    ).fetchone()


@app.route(
    "/member-portal/digital-book/<int:digital_book_id>/create-order",
    methods=["POST"],
)
@member_card_required
def create_digital_book_order(digital_book_id):
    data = request.get_json(silent=True) or {}
    access_type = str(
        data.get("access_type", "read")
    ).strip().lower()

    if access_type not in {"read", "download"}:
        return jsonify({
            "success": False,
            "message": "Invalid digital-book access type.",
        }), 400

    connection = get_db_connection()
    digital_book = _get_digital_book_record(
        connection,
        digital_book_id,
    )

    if digital_book is None:
        connection.close()
        return jsonify({
            "success": False,
            "message": "Digital book is unavailable.",
        }), 404

    if member_has_digital_access(
        connection,
        session["member_id"],
        digital_book_id,
        access_type,
    ):
        connection.close()
        return jsonify({
            "success": True,
            "already_granted": True,
            "open_url": (
                url_for(
                    "digital_book_download",
                    digital_book_id=digital_book_id,
                )
                if access_type == "download"
                else url_for(
                    "digital_book_reader",
                    digital_book_id=digital_book_id,
                )
            ),
        })

    amount = int(
        digital_book[
            "download_price"
            if access_type == "download"
            else "read_price"
        ]
    )

    client = get_razorpay_client()
    if client is None:
        connection.close()
        return jsonify({
            "success": False,
            "message": (
                "Online payment is not configured. Add valid Razorpay "
                "Test keys in the .env file."
            ),
        }), 503

    try:
        order = client.order.create({
            "amount": amount * 100,
            "currency": "INR",
            "receipt": (
                f"ebook_{digital_book_id}_{access_type}_"
                f"{secrets.token_hex(4)}"
            ),
            "notes": {
                "member_id": str(session["member_id"]),
                "digital_book_id": str(digital_book_id),
                "access_type": access_type,
            },
        })

        cursor = connection.execute(
            """
            INSERT INTO digital_book_payments (
                member_id,
                digital_book_id,
                access_type,
                amount,
                payment_status,
                gateway_order_id,
                created_at
            )
            VALUES (
                ?, ?, ?, ?, 'Pending', ?,
                datetime('now', 'localtime')
            )
            """,
            (
                session["member_id"],
                digital_book_id,
                access_type,
                amount,
                order["id"],
            ),
        )
        payment_record_id = cursor.lastrowid
        connection.commit()

        member = connection.execute(
            """
            SELECT *
            FROM members
            WHERE id = ?
            """,
            (session["member_id"],),
        ).fetchone()
    except Exception as error:
        connection.rollback()
        connection.close()
        app.logger.exception(
            "Digital-book order creation failed: %s",
            error,
        )
        return jsonify({
            "success": False,
            "message": "The digital-book payment order could not be created.",
        }), 500

    connection.close()
    return jsonify({
        "success": True,
        "key_id": RAZORPAY_KEY_ID,
        "order_id": order["id"],
        "amount": amount * 100,
        "currency": "INR",
        "payment_record_id": payment_record_id,
        "book_title": digital_book["title"],
        "access_type": access_type,
        "member_name": member["name"],
        "member_email": member["email"] or "",
        "member_phone": member["phone"] or "",
    })


@app.route(
    "/member-portal/digital-book/payment/verify",
    methods=["POST"],
)
@member_card_required
def verify_digital_book_payment():
    data = request.get_json(silent=True) or {}

    try:
        payment_record_id = int(
            data.get("payment_record_id", 0)
        )
    except (TypeError, ValueError):
        payment_record_id = 0

    order_id = str(
        data.get("razorpay_order_id", "")
    ).strip()
    gateway_payment_id = str(
        data.get("razorpay_payment_id", "")
    ).strip()
    signature = str(
        data.get("razorpay_signature", "")
    ).strip()

    if not all((
        payment_record_id,
        order_id,
        gateway_payment_id,
        signature,
    )):
        return jsonify({
            "success": False,
            "message": "Incomplete payment information.",
        }), 400

    connection = get_db_connection()
    payment = connection.execute(
      """
SELECT
    digital_book_payments.*,
    digital_books.is_active,
    digital_books.title
FROM digital_book_payments
JOIN digital_books
    ON digital_books.id = digital_book_payments.digital_book_id
WHERE digital_book_payments.id = ?
  AND digital_book_payments.member_id = ?
  AND digital_book_payments.gateway_order_id = ?
""",
(
    payment_record_id,
    session["member_id"],
    order_id,
),
).fetchone()

    if payment is None:
        connection.close()
        return jsonify({
            "success": False,
            "message": "Digital-book payment record was not found.",
        }), 404

    if payment["payment_status"] == "Paid":
        access_type = payment["access_type"]
        connection.close()
        return jsonify({
            "success": True,
            "open_url": (
                url_for(
                    "digital_book_download",
                    digital_book_id=payment["digital_book_id"],
                )
                if access_type == "download"
                else url_for(
                    "digital_book_reader",
                    digital_book_id=payment["digital_book_id"],
                )
            ),
        })

    client = get_razorpay_client()
    if client is None:
        connection.close()
        return jsonify({
            "success": False,
            "message": "Payment service is unavailable.",
        }), 503

    expected_amount = int(payment["amount"]) * 100

    try:
        client.utility.verify_payment_signature({
            "razorpay_order_id": order_id,
            "razorpay_payment_id": gateway_payment_id,
            "razorpay_signature": signature,
        })

        gateway_payment = client.payment.fetch(
            gateway_payment_id
        )

        if gateway_payment.get("status") == "authorized":
            client.payment.capture(
                gateway_payment_id,
                expected_amount,
            )
            gateway_payment = client.payment.fetch(
                gateway_payment_id
            )

        if (
            gateway_payment.get("order_id") != order_id
            or int(gateway_payment.get("amount", 0)) != expected_amount
            or gateway_payment.get("currency") != "INR"
            or gateway_payment.get("status") != "captured"
        ):
            raise ValueError("Payment amount or status mismatch.")

        connection.execute(
            """
            UPDATE digital_book_payments
            SET payment_status = 'Paid',
                gateway_payment_id = ?,
                gateway_signature = ?,
                paid_at = datetime('now', 'localtime')
            WHERE id = ?
            """,
            (
                gateway_payment_id,
                signature,
                payment_record_id,
            ),
        )

        grant_digital_access(
            connection,
            session["member_id"],
            payment["digital_book_id"],
            payment["access_type"],
            payment_record_id,
        )
        connection.commit()
    except Exception as error:
        connection.rollback()
        connection.close()
        app.logger.warning(
            "Digital-book payment verification failed: %s",
            error,
        )
        return jsonify({
            "success": False,
            "message": "Digital-book payment verification failed.",
        }), 400

    access_type = payment["access_type"]
    digital_book_id = payment["digital_book_id"]
    connection.close()

    return jsonify({
        "success": True,
        "open_url": (
            url_for(
                "digital_book_download",
                digital_book_id=digital_book_id,
            )
            if access_type == "download"
            else url_for(
                "digital_book_reader",
                digital_book_id=digital_book_id,
            )
        ),
    })


@app.route(
    "/member-portal/digital-book/<int:digital_book_id>/reader"
)
@member_card_required
def digital_book_reader(digital_book_id):
    connection = get_db_connection()
    digital_book = _get_digital_book_record(
        connection,
        digital_book_id,
        require_active=False,
    )

    allowed = (
        digital_book is not None
        and member_has_digital_access(
            connection,
            session["member_id"],
            digital_book_id,
            "read",
        )
    )
    connection.close()

    if not allowed:
        flash(
            "Purchase reading access before opening this digital book.",
            "warning",
        )
        return redirect(
            url_for("member_portal") + "#digital-books"
        )

    return render_template(
        "book_reader.html",
        digital_book=digital_book,
    )


@app.route(
    "/member-portal/digital-book/<int:digital_book_id>/content"
)
@member_card_required
def digital_book_content(digital_book_id):
    connection = get_db_connection()
    digital_book = _get_digital_book_record(
        connection,
        digital_book_id,
        require_active=False,
    )

    allowed = (
        digital_book is not None
        and member_has_digital_access(
            connection,
            session["member_id"],
            digital_book_id,
            "read",
        )
    )
    connection.close()

    if not allowed:
        return "Reading access is required.", 403

    pdf_path = private_pdf_path(
        digital_book["pdf_filename"]
    )
    if pdf_path is None:
        return "Digital PDF file is unavailable.", 404

    response = send_file(
        pdf_path,
        mimetype="application/pdf",
        as_attachment=False,
        download_name=f"{digital_book['title']}.pdf",
        conditional=True,
    )
    response.headers["Content-Disposition"] = "inline"
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.route(
    "/member-portal/digital-book/<int:digital_book_id>/download"
)
@member_card_required
def digital_book_download(digital_book_id):
    connection = get_db_connection()
    digital_book = _get_digital_book_record(
        connection,
        digital_book_id,
        require_active=False,
    )

    allowed = (
        digital_book is not None
        and member_has_digital_access(
            connection,
            session["member_id"],
            digital_book_id,
            "download",
        )
    )
    connection.close()

    if not allowed:
        flash(
            "Purchase download access before downloading this book.",
            "warning",
        )
        return redirect(
            url_for("member_portal") + "#digital-books"
        )

    pdf_path = private_pdf_path(
        digital_book["pdf_filename"]
    )
    if pdf_path is None:
        return "Digital PDF file is unavailable.", 404

    return send_file(
        pdf_path,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"{digital_book['title']}.pdf",
    )




# Create and migrate tables for both local Flask and Gunicorn.
create_database()

@app.route("/payment-config-check")
@login_required
def payment_config_check():
    return jsonify({
        "razorpay_package_installed":
            razorpay is not None,

        "configured":
            RAZORPAY_CONFIGURED,

        "key_id_loaded":
            bool(RAZORPAY_KEY_ID),

        "key_secret_loaded":
            bool(RAZORPAY_KEY_SECRET),

        "mode": (
            "Test Mode"
            if RAZORPAY_CONFIGURED
            and RAZORPAY_KEY_ID.startswith(
                "rzp_test_"
            )
            else "Live Mode"
            if RAZORPAY_CONFIGURED
            and RAZORPAY_KEY_ID.startswith(
                "rzp_live_"
            )
            else "Not Configured"
        ),
    })


# -------------------------------------------------
# RUN APPLICATION
# -------------------------------------------------

if __name__ == "__main__":

    create_backup_if_due(
        silent=True
    )

    app.run(
        host="0.0.0.0",
        port=5000,
        debug=True,
    )
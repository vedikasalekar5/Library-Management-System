"""Protected digital-book storage and access helpers for Aureon."""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta
from pathlib import Path

from werkzeug.utils import secure_filename


BASE_DIR = Path(__file__).resolve().parent
_configured_private_dir = os.environ.get(
    "PRIVATE_BOOKS_DIR",
    "",
).strip()
PRIVATE_BOOKS_DIR = Path(
    _configured_private_dir
    or str(BASE_DIR / "private_books")
).resolve()
PRIVATE_BOOKS_DIR.mkdir(parents=True, exist_ok=True)


def save_private_pdf(file_storage, book_id: int) -> str:
    """Validate and save an uploaded PDF outside the public static folder."""
    original_name = secure_filename(
        str(getattr(file_storage, "filename", "") or "")
    )

    if not original_name or not original_name.lower().endswith(".pdf"):
        raise ValueError("Please upload a valid PDF file.")

    stored_name = (
        f"book-{int(book_id)}-{secrets.token_hex(10)}.pdf"
    )
    destination = PRIVATE_BOOKS_DIR / stored_name
    file_storage.save(destination)

    if not destination.is_file() or destination.stat().st_size == 0:
        destination.unlink(missing_ok=True)
        raise ValueError("The uploaded PDF is empty.")

    with destination.open("rb") as uploaded_file:
        signature = uploaded_file.read(5)

    if signature != b"%PDF-":
        destination.unlink(missing_ok=True)
        raise ValueError("The uploaded file is not a valid PDF document.")

    return stored_name


def private_pdf_path(stored_name: str) -> Path | None:
    """Return a safe private PDF path without permitting directory traversal."""
    safe_name = secure_filename(str(stored_name or ""))
    if not safe_name or safe_name != stored_name:
        return None

    candidate = (PRIVATE_BOOKS_DIR / safe_name).resolve()

    try:
        candidate.relative_to(PRIVATE_BOOKS_DIR)
    except ValueError:
        return None

    if not candidate.is_file() or candidate.stat().st_size == 0:
        return None

    return candidate


def delete_private_pdf(stored_name: str) -> None:
    path = private_pdf_path(stored_name)
    if path is not None:
        path.unlink(missing_ok=True)


def member_has_digital_access(
    connection,
    member_id: int,
    digital_book_id: int,
    access_type: str,
) -> bool:
    """Check current read/download access; download access also permits reading."""
    allowed_types = (
        ("read", "download")
        if access_type == "read"
        else ("download",)
    )
    placeholders = ",".join("?" for _ in allowed_types)

    row = connection.execute(
        f"""
        SELECT id
        FROM digital_book_access
        WHERE member_id = ?
          AND digital_book_id = ?
          AND access_type IN ({placeholders})
          AND (
                expires_at IS NULL
                OR datetime(expires_at) > datetime('now', 'localtime')
              )
        ORDER BY id DESC
        LIMIT 1
        """,
        (
            member_id,
            digital_book_id,
            *allowed_types,
        ),
    ).fetchone()

    return row is not None


def grant_digital_access(
    connection,
    member_id: int,
    digital_book_id: int,
    access_type: str,
    payment_id: int,
) -> None:
    """Grant 24-hour reading or permanent download access."""
    if access_type not in {"read", "download"}:
        raise ValueError("Invalid digital-book access type.")

    granted_at = datetime.now().replace(microsecond=0)
    expires_at = None

    if access_type == "read":
        try:
            duration_hours = int(
                os.environ.get(
                    "DIGITAL_READ_ACCESS_HOURS",
                    "24",
                )
            )
        except (TypeError, ValueError):
            duration_hours = 24

        duration_hours = max(1, min(duration_hours, 720))
        expires_at = (
            granted_at + timedelta(hours=duration_hours)
        ).isoformat(sep=" ")

    connection.execute(
        """
        INSERT INTO digital_book_access (
            member_id,
            digital_book_id,
            payment_id,
            access_type,
            granted_at,
            expires_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            member_id,
            digital_book_id,
            payment_id,
            access_type,
            granted_at.isoformat(sep=" "),
            expires_at,
        ),
    )

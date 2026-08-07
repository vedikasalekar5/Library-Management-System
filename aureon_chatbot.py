"""Database-backed Aureon student chatbot."""

from __future__ import annotations

import os
import re
from datetime import date
from typing import Any


STOP_WORDS = {
    "a", "an", "and", "are", "available", "book", "books", "can",
    "do", "does", "for", "have", "i", "in", "is", "it", "library",
    "me", "of", "on", "please", "show", "the", "there", "to", "we",
    "what", "which", "with", "you", "your",
}


def _clean_question(question: Any) -> str:
    return re.sub(
        r"\s+",
        " ",
        str(question or "").strip(),
    )[:500]


def _search_terms(question: str) -> list[str]:
    words = re.findall(
        r"[A-Za-z0-9+#.-]+",
        question.lower(),
    )
    return [
        word
        for word in words
        if len(word) >= 2 and word not in STOP_WORDS
    ][:8]


def _book_search(connection, question: str):
    terms = _search_terms(question)

    if not terms:
        return []

    conditions = []
    values = []

    for term in terms:
        like_value = f"%{term}%"
        conditions.append(
            """
            (
                LOWER(books.title) LIKE ?
                OR LOWER(books.author) LIKE ?
                OR LOWER(COALESCE(books.category, '')) LIKE ?
                OR LOWER(COALESCE(books.isbn, '')) LIKE ?
            )
            """
        )
        values.extend(
            [like_value, like_value, like_value, like_value]
        )

    query = f"""
        SELECT
            books.id,
            books.title,
            books.author,
            books.category,
            books.available_copies,
            books.total_copies,
            CASE
                WHEN digital_books.id IS NOT NULL
                 AND digital_books.is_active = 1
                THEN 1
                ELSE 0
            END AS has_digital_copy,
            digital_books.read_price,
            digital_books.download_price
        FROM books
        LEFT JOIN digital_books
          ON digital_books.book_id = books.id
        WHERE {' OR '.join(conditions)}
        ORDER BY
            books.available_copies DESC,
            books.title
        LIMIT 6
    """

    return connection.execute(
        query,
        values,
    ).fetchall()


def answer_student_question(
    connection,
    member_id: int,
    question: Any,
) -> dict[str, Any]:
    """Return a safe, library-specific answer for a logged-in student."""
    cleaned_question = _clean_question(question)
    lowered = cleaned_question.lower()

    default_suggestions = [
        "Is Operating System available?",
        "Show my issued books",
        "When is my next due date?",
        "Do I have any unpaid fine?",
    ]

    if not cleaned_question:
        return {
            "answer": "Please type a library question.",
            "suggestions": default_suggestions,
            "books": [],
        }

    if (
        re.search(r"\b(?:hello|hi|hey)\b", lowered)
        or "good morning" in lowered
        or "good evening" in lowered
    ):
        return {
            "answer": (
                "Hello! I am Aureon. I can help you find books, check "
                "availability, view your due dates, fines and digital books."
            ),
            "suggestions": default_suggestions,
            "books": [],
        }

    if "timing" in lowered or "open" in lowered or "close" in lowered:
        timings = os.environ.get(
            "LIBRARY_TIMINGS",
            "Monday to Saturday, 9:00 AM to 5:00 PM",
        ).strip()
        return {
            "answer": f"RSIET Library timings: {timings}.",
            "suggestions": [
                "Show available books",
                "When is my next due date?",
            ],
            "books": [],
        }

    if "librarian" in lowered or "contact" in lowered:
        librarian_name = os.environ.get(
            "LIBRARIAN_DISPLAY_NAME",
            "RSIET Librarian",
        ).strip()
        contact = os.environ.get(
            "LIBRARY_CONTACT",
            "Please visit the library help desk.",
        ).strip()
        return {
            "answer": f"{librarian_name}: {contact}",
            "suggestions": default_suggestions,
            "books": [],
        }

    if (
        "my book" in lowered
        or "issued" in lowered
        or "borrow" in lowered
        or "due date" in lowered
        or "next due" in lowered
        or "overdue" in lowered
    ):
        rows = connection.execute(
            """
            SELECT
                books.title,
                books.author,
                transactions.issue_date,
                transactions.due_date,
                CASE
                    WHEN date(transactions.due_date) < date('now', 'localtime')
                    THEN 1
                    ELSE 0
                END AS is_overdue
            FROM transactions
            JOIN books
              ON books.id = transactions.book_id
            WHERE transactions.member_id = ?
              AND transactions.status = 'Issued'
            ORDER BY transactions.due_date
            """,
            (member_id,),
        ).fetchall()

        if not rows:
            return {
                "answer": "You currently have no issued books.",
                "suggestions": [
                    "Show available books",
                    "Show paid digital books",
                ],
                "books": [],
            }

        lines = []
        for row in rows[:6]:
            status = "overdue" if row["is_overdue"] else "due"
            lines.append(
                f"• {row['title']} — {status} on {row['due_date']}"
            )

        return {
            "answer": "Your current issued books:\n" + "\n".join(lines),
            "suggestions": [
                "Do I have any unpaid fine?",
                "Show available books",
            ],
            "books": [],
        }

    if "fine" in lowered or "payment" in lowered or "penalty" in lowered:
        amount = connection.execute(
            """
            SELECT COALESCE(SUM(transactions.fine), 0)
            FROM transactions
            LEFT JOIN fine_payments
              ON fine_payments.transaction_id = transactions.id
            WHERE transactions.member_id = ?
              AND transactions.fine > 0
              AND fine_payments.id IS NULL
            """,
            (member_id,),
        ).fetchone()[0]

        return {
            "answer": (
                f"Your unpaid late-return fine is ₹{int(amount or 0)}. "
                "Open Fines & Payments in your Student Portal for details."
            ),
            "suggestions": [
                "Show my issued books",
                "When is my next due date?",
            ],
            "books": [],
        }

    if any(
        phrase in lowered
        for phrase in {
            "available books",
            "list books",
            "which books",
            "what books",
            "books are there",
            "show books",
        }
    ):
        rows = connection.execute(
            """
            SELECT
                books.title,
                books.author,
                books.category,
                books.available_copies,
                books.total_copies,
                CASE
                    WHEN digital_books.id IS NOT NULL
                     AND digital_books.is_active = 1
                    THEN 1
                    ELSE 0
                END AS has_digital_copy,
                digital_books.read_price,
                digital_books.download_price
            FROM books
            LEFT JOIN digital_books
              ON digital_books.book_id = books.id
            ORDER BY
                books.available_copies DESC,
                books.title
            LIMIT 8
            """
        ).fetchall()

        if not rows:
            return {
                "answer": "No books have been added to the library catalogue yet.",
                "suggestions": default_suggestions,
                "books": [],
            }

        return {
            "answer": (
                "Here are some books currently listed in RSIET Library. "
                "You can ask me about any title or author."
            ),
            "suggestions": [
                "Show paid digital books",
                "Show my issued books",
            ],
            "books": [dict(row) for row in rows],
        }

    if "digital" in lowered or "read online" in lowered or "download" in lowered:
        rows = connection.execute(
            """
            SELECT
                books.title,
                books.author,
                digital_books.read_price,
                digital_books.download_price,
                books.available_copies,
                books.total_copies,
                1 AS has_digital_copy
            FROM digital_books
            JOIN books
              ON books.id = digital_books.book_id
            WHERE digital_books.is_active = 1
            ORDER BY books.title
            LIMIT 6
            """
        ).fetchall()

        if not rows:
            return {
                "answer": "No paid digital books are published yet.",
                "suggestions": default_suggestions,
                "books": [],
            }

        books = [dict(row) for row in rows]
        return {
            "answer": (
                "These digital books are available. Reading access and "
                "download access are purchased separately."
            ),
            "suggestions": [
                "Is Operating System available?",
                "Show my issued books",
            ],
            "books": books,
        }

    book_rows = _book_search(connection, cleaned_question)

    if book_rows:
        books = [dict(row) for row in book_rows]
        first = books[0]
        availability = (
            f"{first['available_copies']} physical copy/copies available"
            if first["available_copies"] > 0
            else "no physical copy currently available"
        )
        digital_text = (
            f" Digital reading is available for ₹{first['read_price']}"
            f" and download for ₹{first['download_price']}."
            if first.get("has_digital_copy")
            else ""
        )
        return {
            "answer": (
                f"{first['title']} by {first['author']} has {availability}."
                f"{digital_text}"
            ),
            "suggestions": [
                "Show my issued books",
                "Show paid digital books",
            ],
            "books": books,
        }

    total_available = connection.execute(
        """
        SELECT COUNT(*)
        FROM books
        WHERE available_copies > 0
        """
    ).fetchone()[0]

    return {
        "answer": (
            "I could not find an exact match. Try typing a book title, "
            f"author or subject. The library currently has {total_available} "
            "titles with an available physical copy."
        ),
        "suggestions": default_suggestions,
        "books": [],
    }

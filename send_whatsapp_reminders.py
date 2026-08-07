"""Daily automatic due and overdue WhatsApp reminder job for Aureon."""

from datetime import date, datetime
import os

from app import FINE_AMOUNT, get_db_connection
from whatsapp_service import send_and_log_whatsapp


def send_due_and_overdue_reminders():
    today = date.today()
    connection = get_db_connection()

    rows = connection.execute(
        """
        SELECT
            transactions.id AS transaction_id,
            transactions.due_date,
            books.title,
            members.*
        FROM transactions
        JOIN books
          ON books.id = transactions.book_id
        JOIN members
          ON members.id = transactions.member_id
        WHERE transactions.status = 'Issued'
          AND COALESCE(members.whatsapp_opt_in, 0) = 1
          AND TRIM(COALESCE(members.phone, '')) != ''
        ORDER BY transactions.due_date
        """
    ).fetchall()

    submitted = 0
    failed = 0
    skipped = 0

    for row in rows:
        try:
            due_on = date.fromisoformat(row["due_date"])
        except (TypeError, ValueError):
            skipped += 1
            continue

        days_remaining = (due_on - today).days
        template_name = None
        notification_type = None
        parameters = None
        unique_key = None

        if days_remaining == 2:
            template_name = os.environ.get(
                "WHATSAPP_TEMPLATE_BOOK_DUE_SOON",
                "rsiet_book_due_soon",
            ).strip()
            notification_type = "Due Soon"
            parameters = [
                row["name"],
                row["title"],
                due_on.strftime("%d-%m-%Y"),
            ]
            unique_key = f"due-soon:{row['transaction_id']}"
        elif days_remaining == 0:
            template_name = os.environ.get(
                "WHATSAPP_TEMPLATE_BOOK_DUE_TODAY",
                "rsiet_book_due_today",
            ).strip()
            notification_type = "Due Today"
            parameters = [row["name"], row["title"]]
            unique_key = f"due-today:{row['transaction_id']}"
        elif days_remaining < 0:
            overdue_days = abs(days_remaining)
            template_name = os.environ.get(
                "WHATSAPP_TEMPLATE_BOOK_OVERDUE",
                "rsiet_book_overdue",
            ).strip()
            notification_type = "Overdue"
            parameters = [
                row["name"],
                row["title"],
                str(overdue_days),
                str(FINE_AMOUNT),
            ]
            unique_key = (
                f"overdue:{row['transaction_id']}:{today.isoformat()}"
            )
        else:
            skipped += 1
            continue

        result = send_and_log_whatsapp(
            connection,
            row,
            notification_type=notification_type,
            unique_key=unique_key,
            template_name=template_name,
            body_parameters=parameters,
            transaction_id=row["transaction_id"],
        )

        if result.get("skipped"):
            skipped += 1
        elif result.get("success"):
            submitted += 1
            print(
                "WhatsApp submitted:",
                row["name"],
                row["title"],
                notification_type,
            )
        else:
            failed += 1
            print(
                "WhatsApp failed:",
                row["name"],
                result.get("error", "Unknown error"),
            )

    connection.close()

    print(
        "Reminder check completed:",
        datetime.now().strftime("%d-%m-%Y %H:%M:%S"),
    )
    print(
        "Submitted:", submitted,
        "| Failed:", failed,
        "| Skipped:", skipped,
    )


if __name__ == "__main__":
    send_due_and_overdue_reminders()

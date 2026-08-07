"""WhatsApp Cloud API helpers for Aureon automatic library messages."""

from __future__ import annotations

import os
from typing import Any, Iterable

import requests

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv():
        return False


load_dotenv()


def normalise_whatsapp_phone(phone: Any) -> str | None:
    """Convert an Indian/local number to international digits for WhatsApp."""
    digits = "".join(
        character
        for character in str(phone or "")
        if character.isdigit()
    )

    if digits.startswith("00"):
        digits = digits[2:]

    if len(digits) == 10:
        digits = "91" + digits
    elif len(digits) == 11 and digits.startswith("0"):
        digits = "91" + digits[1:]

    if not 10 <= len(digits) <= 15:
        return None

    return digits


def _environment_value(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def whatsapp_is_configured() -> bool:
    enabled = _environment_value(
        "WHATSAPP_ENABLED",
        "false",
    ).lower() in {"1", "true", "yes", "on"}

    token = _environment_value("WHATSAPP_ACCESS_TOKEN")
    phone_number_id = _environment_value(
        "WHATSAPP_PHONE_NUMBER_ID"
    )
    api_version = _environment_value(
        "WHATSAPP_API_VERSION"
    )

    placeholders = {
        "",
        "PASTE_YOUR_ACCESS_TOKEN",
        "PASTE_YOUR_PHONE_NUMBER_ID",
        "PASTE_VERSION_SHOWN_IN_META",
    }

    return (
        enabled
        and token not in placeholders
        and phone_number_id not in placeholders
        and api_version not in placeholders
    )


def send_whatsapp_template(
    phone: Any,
    template_name: str,
    body_parameters: Iterable[Any],
) -> dict[str, Any]:
    """Send one approved WhatsApp utility template."""
    if not whatsapp_is_configured():
        return {
            "success": False,
            "message_id": "",
            "error": (
                "WhatsApp Cloud API is not configured. "
                "Add valid values to the .env file."
            ),
        }

    recipient = normalise_whatsapp_phone(phone)

    if not recipient:
        return {
            "success": False,
            "message_id": "",
            "error": "The student phone number is invalid.",
        }

    api_version = _environment_value(
        "WHATSAPP_API_VERSION"
    )
    phone_number_id = _environment_value(
        "WHATSAPP_PHONE_NUMBER_ID"
    )
    access_token = _environment_value(
        "WHATSAPP_ACCESS_TOKEN"
    )
    language_code = _environment_value(
        "WHATSAPP_TEMPLATE_LANGUAGE",
        "en_US",
    )

    endpoint = (
        "https://graph.facebook.com/"
        f"{api_version}/{phone_number_id}/messages"
    )

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {
                "code": language_code,
            },
            "components": [
                {
                    "type": "body",
                    "parameters": [
                        {
                            "type": "text",
                            "text": str(value),
                        }
                        for value in body_parameters
                    ],
                }
            ],
        },
    }

    try:
        response = requests.post(
            endpoint,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=20,
        )
    except requests.RequestException as error:
        return {
            "success": False,
            "message_id": "",
            "error": str(error),
        }

    try:
        response_data = response.json()
    except ValueError:
        response_data = {}

    if not response.ok:
        error_message = (
            response_data
            .get("error", {})
            .get(
                "message",
                f"WhatsApp returned HTTP {response.status_code}.",
            )
        )
        return {
            "success": False,
            "message_id": "",
            "error": error_message,
        }

    messages = response_data.get("messages", [])
    message_id = (
        messages[0].get("id", "")
        if messages
        else ""
    )

    return {
        "success": True,
        "message_id": message_id,
        "error": "",
    }


def send_and_log_whatsapp(
    connection,
    member,
    *,
    notification_type: str,
    unique_key: str,
    template_name: str,
    body_parameters: Iterable[Any],
    transaction_id: int | None = None,
) -> dict[str, Any]:
    """Send an opted-in message and keep a duplicate-safe database log."""
    if member is None:
        return {
            "success": False,
            "skipped": True,
            "error": "Member was not found.",
        }

    try:
        opted_in = int(member["whatsapp_opt_in"] or 0) == 1
    except (KeyError, IndexError, TypeError, ValueError):
        opted_in = False

    if not opted_in:
        return {
            "success": False,
            "skipped": True,
            "error": "Student has not opted in to WhatsApp messages.",
        }

    if not whatsapp_is_configured():
        return {
            "success": False,
            "skipped": True,
            "error": (
                "WhatsApp Cloud API is not configured. "
                "Add valid values to the .env file."
            ),
        }

    phone = normalise_whatsapp_phone(member["phone"])

    if not phone:
        return {
            "success": False,
            "skipped": True,
            "error": "Student phone number is invalid.",
        }

    existing = connection.execute(
        """
        SELECT delivery_status
        FROM whatsapp_message_log
        WHERE unique_key = ?
        """,
        (unique_key,),
    ).fetchone()

    if (
        existing is not None
        and existing["delivery_status"] in {
            "Submitted",
            "Delivered",
            "Read",
        }
    ):
        return {
            "success": True,
            "skipped": True,
            "error": "",
        }

    result = send_whatsapp_template(
        phone=phone,
        template_name=template_name,
        body_parameters=body_parameters,
    )

    delivery_status = (
        "Submitted"
        if result.get("success")
        else "Failed"
    )

    connection.execute(
        """
        INSERT INTO whatsapp_message_log (
            member_id,
            transaction_id,
            notification_type,
            unique_key,
            phone,
            template_name,
            message_id,
            delivery_status,
            error_message,
            sent_at
        )
        VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?,
            datetime('now', 'localtime')
        )
        ON CONFLICT(unique_key)
        DO UPDATE SET
            phone = excluded.phone,
            template_name = excluded.template_name,
            message_id = excluded.message_id,
            delivery_status = excluded.delivery_status,
            error_message = excluded.error_message,
            sent_at = datetime('now', 'localtime')
        """,
        (
            member["id"],
            transaction_id,
            notification_type,
            unique_key,
            phone,
            template_name,
            result.get("message_id") or None,
            delivery_status,
            result.get("error") or None,
        ),
    )
    connection.commit()

    result["skipped"] = False
    return result

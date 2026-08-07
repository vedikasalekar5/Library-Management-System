"""Secure email delivery for the Aureon Librarian OTP gate."""

import os
import smtplib
import ssl
from email.message import EmailMessage

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv():
        return False


load_dotenv()


def _smtp_port():
    try:
        return int(
            os.environ.get(
                "OTP_SMTP_PORT",
                "465",
            )
        )
    except (TypeError, ValueError):
        return 465


def send_librarian_otp(
    recipient_email,
    otp_code,
    expiry_minutes,
):
    """
    Send one Aureon Librarian OTP email.

    Returns a dictionary with ``success`` and ``error`` keys.
    The OTP is never printed or written to an application log.
    """
    sender_email = os.environ.get(
        "OTP_SENDER_EMAIL",
        "",
    ).strip()
    app_password = os.environ.get(
        "OTP_SENDER_APP_PASSWORD",
        "",
    ).replace(" ", "").strip()
    smtp_host = os.environ.get(
        "OTP_SMTP_HOST",
        "smtp.gmail.com",
    ).strip()
    smtp_port = _smtp_port()

    placeholder_values = {
        "",
        "ADD_GMAIL_APP_PASSWORD_HERE",
        "PASTE_GMAIL_APP_PASSWORD_HERE",
        "YOUR_GMAIL_APP_PASSWORD",
    }

    if not sender_email:
        return {
            "success": False,
            "error": (
                "OTP_SENDER_EMAIL is missing in the .env file."
            ),
        }

    if app_password in placeholder_values:
        return {
            "success": False,
            "error": (
                "Add the Gmail App Password to "
                "OTP_SENDER_APP_PASSWORD in the .env file."
            ),
        }

    if not recipient_email:
        return {
            "success": False,
            "error": "The OTP recipient email is missing.",
        }

    message = EmailMessage()
    message["Subject"] = (
        f"{otp_code} is your Aureon Librarian security code"
    )
    message["From"] = (
        f"RSIET Library - Aureon <{sender_email}>"
    )
    message["To"] = recipient_email

    plain_text = (
        "RSIET Library - Aureon\n\n"
        f"Your Librarian verification code is: {otp_code}\n\n"
        f"This code expires in {expiry_minutes} minute(s).\n"
        "Do not share this code with anyone.\n\n"
        "If you did not request this code, ignore this email."
    )

    html_text = f"""
    <!doctype html>
    <html lang="en">
    <body style="margin:0;background:#f5f3ff;font-family:Arial,sans-serif;color:#292451;">
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="padding:28px 12px;background:#f5f3ff;">
            <tr>
                <td align="center">
                    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:560px;background:#ffffff;border:1px solid #e3def5;border-radius:20px;overflow:hidden;box-shadow:0 18px 45px rgba(49,46,129,.12);">
                        <tr>
                            <td style="padding:28px;background:linear-gradient(135deg,#312e81,#4f46e5,#7c3aed);color:#ffffff;">
                                <div style="font-size:12px;font-weight:700;letter-spacing:1.4px;color:#ddd6fe;">RAJARAM SHINDE INSTITUTE OF TECHNOLOGY, PEDHAMBE</div>
                                <div style="margin-top:8px;font-size:26px;font-weight:800;">Aureon Librarian Security</div>
                            </td>
                        </tr>
                        <tr>
                            <td style="padding:30px;">
                                <p style="margin:0 0 12px;font-size:16px;">Use this one-time verification code to continue:</p>
                                <div style="margin:22px 0;padding:18px;border-radius:14px;background:#f1efff;color:#312e81;text-align:center;font-size:34px;font-weight:900;letter-spacing:10px;">{otp_code}</div>
                                <p style="margin:0 0 8px;font-size:14px;line-height:1.6;">The code expires in <strong>{expiry_minutes} minute(s)</strong>.</p>
                                <p style="margin:0;color:#77729b;font-size:13px;line-height:1.6;">Do not share this code. RSIET Library will never ask you to send the OTP by message or phone.</p>
                            </td>
                        </tr>
                        <tr>
                            <td style="padding:18px 30px;background:#faf9ff;color:#77729b;font-size:12px;text-align:center;">Aureon Library Management System</td>
                        </tr>
                    </table>
                </td>
            </tr>
        </table>
    </body>
    </html>
    """

    message.set_content(plain_text)
    message.add_alternative(
        html_text,
        subtype="html",
    )

    ssl_context = ssl.create_default_context()

    try:
        if smtp_port == 465:
            with smtplib.SMTP_SSL(
                smtp_host,
                smtp_port,
                context=ssl_context,
                timeout=20,
            ) as smtp_connection:
                smtp_connection.login(
                    sender_email,
                    app_password,
                )
                smtp_connection.send_message(
                    message
                )
        else:
            with smtplib.SMTP(
                smtp_host,
                smtp_port,
                timeout=20,
            ) as smtp_connection:
                smtp_connection.ehlo()
                smtp_connection.starttls(
                    context=ssl_context
                )
                smtp_connection.ehlo()
                smtp_connection.login(
                    sender_email,
                    app_password,
                )
                smtp_connection.send_message(
                    message
                )

        return {
            "success": True,
            "error": "",
        }

    except smtplib.SMTPAuthenticationError:
        return {
            "success": False,
            "error": (
                "Gmail rejected the login. Use a valid Gmail "
                "App Password, not the normal Gmail password."
            ),
        }
    except (
        smtplib.SMTPException,
        OSError,
    ) as error:
        return {
            "success": False,
            "error": (
                "The OTP email could not be sent: "
                f"{type(error).__name__}."
            ),
        }

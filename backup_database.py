from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path


# =========================================
# BACKUP SETTINGS
# =========================================

BASE_DIR = Path(__file__).resolve().parent

DATABASE_FILE = BASE_DIR / "library.db"

BACKUP_FOLDER = BASE_DIR / "backups"

LOG_FILE = BACKUP_FOLDER / "backup_log.txt"


# Create a backup after every 60 days

BACKUP_INTERVAL_DAYS = 60


# Keep only the latest 5 backups

KEEP_LATEST_BACKUPS = 5


BACKUP_NAME_PREFIX = "aureon_auto_backup_"


# =========================================
# WRITE BACKUP LOG
# =========================================

def write_log(message):
    BACKUP_FOLDER.mkdir(
        parents=True,
        exist_ok=True
    )

    current_time = datetime.now().strftime(
        "%d-%m-%Y %I:%M:%S %p"
    )

    with LOG_FILE.open(
        "a",
        encoding="utf-8"
    ) as log_file:

        log_file.write(
            f"[{current_time}] {message}\n"
        )


# =========================================
# GET ALL BACKUP FILES
# =========================================

def get_backup_files():
    if not BACKUP_FOLDER.exists():
        return []

    backup_files = list(
        BACKUP_FOLDER.glob(
            f"{BACKUP_NAME_PREFIX}*.db"
        )
    )

    backup_files.sort(
        key=lambda file:
            file.stat().st_mtime,
        reverse=True
    )

    return backup_files


# =========================================
# GET LATEST BACKUP
# =========================================

def get_latest_backup():
    backup_files = get_backup_files()

    if not backup_files:
        return None

    return backup_files[0]


# =========================================
# CALCULATE NEXT BACKUP DATE
# =========================================

def get_next_backup_date():
    latest_backup = get_latest_backup()

    if latest_backup is None:
        return datetime.now()

    latest_backup_time = datetime.fromtimestamp(
        latest_backup.stat().st_mtime
    )

    return (
        latest_backup_time
        + timedelta(
            days=BACKUP_INTERVAL_DAYS
        )
    )


# =========================================
# CHECK WHETHER BACKUP IS DUE
# =========================================

def backup_is_due():
    return (
        datetime.now()
        >= get_next_backup_date()
    )


# =========================================
# VALIDATE BACKUP DATABASE
# =========================================

def validate_database(database_path):
    connection = sqlite3.connect(
        str(database_path)
    )

    try:
        result = connection.execute(
            "PRAGMA integrity_check"
        ).fetchone()

        if (
            result is None
            or result[0].lower() != "ok"
        ):
            raise RuntimeError(
                "Database integrity check failed."
            )


        required_tables = {
            "books",
            "members",
            "transactions"
        }


        table_rows = connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
            """
        ).fetchall()


        available_tables = {
            row[0]
            for row in table_rows
        }


        missing_tables = (
            required_tables
            - available_tables
        )


        if missing_tables:
            missing_text = ", ".join(
                sorted(missing_tables)
            )

            raise RuntimeError(
                "Backup is missing tables: "
                f"{missing_text}"
            )

    finally:
        connection.close()


# =========================================
# DELETE OLD BACKUPS
# =========================================

def delete_old_backups():
    backup_files = get_backup_files()


    old_backup_files = backup_files[
        KEEP_LATEST_BACKUPS:
    ]


    for old_backup in old_backup_files:
        try:
            old_backup.unlink()

            write_log(
                "Deleted old backup: "
                f"{old_backup.name}"
            )

        except OSError as error:
            write_log(
                "Could not delete old backup: "
                f"{error}"
            )


# =========================================
# CREATE DATABASE BACKUP
# =========================================

def create_database_backup(force=False):

    BACKUP_FOLDER.mkdir(
        parents=True,
        exist_ok=True
    )


    if not DATABASE_FILE.exists():
        message = (
            "library.db was not found."
        )

        write_log(message)

        raise FileNotFoundError(
            message
        )


    # Do not create another backup before 60 days

    if not force and not backup_is_due():

        next_backup_date = (
            get_next_backup_date()
        )

        message = (
            "Backup is not due. "
            "Next backup date: "
            f"{next_backup_date.strftime('%d-%m-%Y')}"
        )

        print(message)

        return None


    timestamp = datetime.now().strftime(
        "%Y-%m-%d_%H-%M-%S"
    )


    backup_file = (
        BACKUP_FOLDER
        / (
            f"{BACKUP_NAME_PREFIX}"
            f"{timestamp}.db"
        )
    )


    temporary_file = (
        BACKUP_FOLDER
        / (
            f".temporary_backup_"
            f"{timestamp}.db"
        )
    )


    source_connection = None
    backup_connection = None


    try:

        source_connection = sqlite3.connect(
            str(DATABASE_FILE),
            timeout=30
        )


        backup_connection = sqlite3.connect(
            str(temporary_file)
        )


        # Safely copy the SQLite database

        source_connection.backup(
            backup_connection
        )


        backup_connection.commit()

        backup_connection.close()

        backup_connection = None


        # Check the copied database

        validate_database(
            temporary_file
        )


        # Rename temporary file after validation

        os.replace(
            temporary_file,
            backup_file
        )


        # Remove older backups

        delete_old_backups()


        message = (
            "Backup created successfully: "
            f"{backup_file.name}"
        )


        print(message)

        write_log(message)


        return backup_file


    except Exception as error:

        if temporary_file.exists():
            try:
                temporary_file.unlink()

            except OSError:
                pass


        message = (
            "Automatic backup failed: "
            f"{error}"
        )


        print(message)

        write_log(message)


        raise


    finally:

        if backup_connection is not None:
            backup_connection.close()


        if source_connection is not None:
            source_connection.close()


# =========================================
# SAFE FUNCTION FOR APP.PY
# =========================================

def create_backup_if_due(silent=False):

    try:
        return create_database_backup(
            force=False
        )

    except Exception as error:

        if not silent:
            print(
                "Backup check failed:",
                error
            )

        return None


# =========================================
# RUN BACKUP FILE
# =========================================

if __name__ == "__main__":

    force_backup = (
        "--force" in sys.argv
    )


    try:

        created_backup = (
            create_database_backup(
                force=force_backup
            )
        )


        if created_backup is not None:

            print(
                "Backup saved at:",
                created_backup
            )


    except Exception:

        sys.exit(1)
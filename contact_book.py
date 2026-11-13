#!/usr/bin/python3

########################################################################
## Description: Manage and import contacts in a local CSV contact book
##
## Author: Matteo Z.
########################################################################

import csv
import logging
import os
import re
import shutil
import sys
import tempfile
from argparse import ArgumentParser, RawDescriptionHelpFormatter
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator


LOGGER = logging.getLogger("contact_book")
DEFAULT_FILE = "ContactBook.csv"


class ContactDataError(ValueError):
    """Report an invalid contact book or import record."""


def validate_phone_number(value: str) -> bool:
    return bool(
        re.fullmatch(r"\+?[0-9 ()-]+", value)
        and 7 <= len(re.findall(r"[0-9]", value)) <= 15
    )


def validate_contact(fields, line_number):
    if len(fields) not in (3, 4):
        raise ContactDataError(f"line {line_number}: expected 3 or 4 CSV columns, found {len(fields)}")

    fields = [field.strip() for field in fields]

    if not fields[0] or not fields[1]:
        raise ContactDataError(f"line {line_number}: surname and name cannot be empty")

    if any("\n" in field or "\r" in field or "\x00" in field for field in fields):
        raise ContactDataError(f"line {line_number}: contact fields contain invalid control characters")

    if not validate_phone_number(fields[2]):
        raise ContactDataError(f"line {line_number}: invalid phone number")

    fields[0] = fields[0].upper()
    fields[1] = fields[1].upper()

    return fields


def contact_key(contact):
    return contact[0].upper().casefold(), contact[1].upper().casefold()


def read_contacts(path, required=False):
    if not path.exists():
        if required:
            raise FileNotFoundError(f"CSV file not found: {path}")

        return []

    if not path.is_file():
        raise ContactDataError(f"not a regular CSV file: {path}")

    contacts = []
    seen = set()
    is_first_record = True

    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            if "\x00" in stream.read(4096):
                raise ContactDataError("CSV contains binary data")

            stream.seek(0)
            reader = csv.reader(stream, delimiter=";", skipinitialspace=True, strict=True)

            for fields in reader:
                if not fields or all(not field.strip() for field in fields):
                    continue

                # Header is optional; the original CSV format has no header.
                header = [field.strip().lower().replace(" ", "_") for field in fields]

                if is_first_record and header[:3] in (
                    ["surname", "name", "phone_number"],
                    ["surname", "name", "phone"],
                ):
                    is_first_record = False
                    continue

                is_first_record = False
                contact = validate_contact(fields, reader.line_num)
                key = contact_key(contact)

                if key in seen:
                    raise ContactDataError(f"line {reader.line_num}: duplicate contact {contact[0]} {contact[1]}")

                seen.add(key)
                contacts.append(contact)
    except csv.Error as error:
        raise ContactDataError(f"invalid CSV syntax near line {reader.line_num}: {error}") from error
    except UnicodeError as error:
        raise ContactDataError("CSV file must be UTF-8 encoded text") from error

    return contacts


def save_contacts(path, contacts):
    temporary_path = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            writer = csv.writer(stream, delimiter=";", lineterminator="\n")
            writer.writerows(sorted(contacts, key=contact_key))
            stream.flush()
            os.fsync(stream.fileno())

        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def show_contacts(contacts):
    if not contacts:
        print("No contacts found.")
        return

    headings = ("SURNAME", "NAME", "PHONE", "DESCRIPTION")
    rows = [(item[0], item[1], item[2], item[3] if len(item) == 4 else "") for item in contacts]
    widths = [max(len(headings[i]), *(len(row[i]) for row in rows)) for i in range(4)]
    line_format = "  ".join(f"{{:<{width}}}" for width in widths)
    print(line_format.format(*headings))
    print("  ".join("-" * width for width in widths))

    for row in rows:
        print(line_format.format(*row).rstrip())

    print(f"\n{len(rows)} contact(s) found.")


def find_matches(contacts, surname, name=None):
    return [contact for contact in contacts if contact[0].upper().casefold() == surname.upper().casefold()
            and (name is None or contact[1].upper().casefold() == name.upper().casefold())]


def prompt_confirmation(message):
    while True:
        answer = input(f"{message} [y/N]: ").strip().lower()

        if answer in ("", "n", "no"):
            return False
        if answer in ("y", "yes"):
            return True

        print("Please answer y or n.")


def import_contacts(path, source_path):
    if source_path.suffix.lower() != ".csv":
        raise ContactDataError("import source must have a .csv extension")

    incoming = read_contacts(source_path, required=True)

    if not incoming:
        raise ContactDataError("import source contains no contacts")

    existing = read_contacts(path)
    by_key = {contact_key(contact): index for index, contact in enumerate(existing)}
    added = updated = unchanged = skipped = 0

    for contact in incoming:
        key = contact_key(contact)

        if key not in by_key:
            existing.append(contact)
            by_key[key] = len(existing) - 1
            added += 1
            continue

        current_index = by_key[key]
        current = existing[current_index]

        if current[2] == contact[2] and (current[3] if len(current) == 4 else "") == (contact[3] if len(contact) == 4 else ""):
            unchanged += 1
            continue

        print(f"\nExisting contact: {contact[0]} {contact[1]}")
        print(f"  Phone:       {current[2]} -> {contact[2]}")
        print(f"  Description: {(current[3] if len(current) == 4 else '') or '(empty)'} -> {(contact[3] if len(contact) == 4 else '') or '(empty)'}\n")

        if prompt_confirmation("Update this contact?"):
            existing[current_index] = contact
            updated += 1
        else:
            skipped += 1

    if added or updated:
        save_contacts(path, existing)

    print(f"Import complete: {added} added, {updated} updated, {unchanged} unchanged, {skipped} skipped.")
    LOGGER.info("IMPORT | added=%d updated=%d unchanged=%d skipped=%d", added, updated, unchanged, skipped)

    return 0


def create_backup(path):
    if not path.is_file():
        raise FileNotFoundError(f"CSV file not found: {path}")

    suffix = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    destination = path.with_name(f"{path.stem}-backup-{suffix}{path.suffix or '.csv'}")
    file_descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)

    try:
        with os.fdopen(file_descriptor, "wb") as target, path.open("rb") as source:
            shutil.copyfileobj(source, target)
            target.flush()
            os.fsync(target.fileno())
    except OSError:
        try:
            destination.unlink()
        except FileNotFoundError:
            pass

        raise

    print(f"Backup created: {destination}")
    LOGGER.info("BACKUP | success")

    return 0


def validate_arguments(parser, args):
    operation = next(name for name in ("add", "mod", "rem", "search", "list", "backup", "import_file") if getattr(args, name))
    values = getattr(args, operation)

    if operation in ("list", "backup", "import_file"):
        return operation, values

    count = len(values)

    if operation == "mod":
        if count == 0 or values[0] not in ("t", "c", "b"):
            parser.error("--mod requires mode t, c or b")

        if count != (5 if values[0] == "b" else 4):
            parser.error("--mod requires 4 arguments (t/c) or 5 arguments (b), including the mode")
    elif count not in {"add": (3, 4), "rem": (1, 2), "search": (1, 2)}[operation]:
        parser.error(f"--{operation} has an invalid number of arguments")

    values = [value.strip() for value in values]

    if any(";" in value or "\r" in value or "\n" in value or "\x00" in value for value in values):
        parser.error("contact arguments cannot contain semicolons or control characters")

    name_offset = 1 if operation == "mod" else 0
    name_count = 1 if operation in ("rem", "search") and len(values) == 1 else 2

    if not all(values[name_offset:name_offset + name_count]):
        parser.error("surname and name cannot be empty")

    phone = values[2] if operation == "add" else values[3] if operation == "mod" and values[0] in ("t", "b") else None

    if phone is not None and not validate_phone_number(phone):
        parser.error("invalid phone number: use 7 to 15 digits, optional leading +, spaces, hyphens or parentheses")

    return operation, values


def run_operation(operation, values, path):
    if operation == "import_file":
        return import_contacts(path, Path(values))
    
    if operation == "backup":
        return create_backup(path)

    if not path.exists():
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)

    contacts = read_contacts(path)

    if operation == "list":
        show_contacts(contacts)
        LOGGER.info("LIST | matches=%d", len(contacts))

        return 0

    if operation == "search":
        matches = find_matches(contacts, *values)
        show_contacts(matches)
        LOGGER.info("SEARCH | matches=%d", len(matches))

        return 0 if matches else 1

    if operation == "add":
        surname, name, phone = values[:3]

        if find_matches(contacts, surname, name):
            print("Contact already exists.")
            LOGGER.warning("ADD | duplicate contact")

            return 1

        contact = [surname.upper(), name.upper(), phone]

        if len(values) == 4:
            contact.append(values[3])

        contacts.append(contact)
        save_contacts(path, contacts)
        print("Contact added successfully.")
        LOGGER.info("ADD | success")

        return 0

    if operation == "mod":
        mode, surname, name = values[:3]
        matches = find_matches(contacts, surname, name)

        if not matches:
            print("Contact not found.")
            LOGGER.warning("MODIFY | contact not found")

            return 1

        contact = matches[0]

        if mode in ("t", "b"):
            contact[2] = values[3]

        if mode in ("c", "b"):
            description = values[3] if mode == "c" else values[4]
            contact[3:] = [description]

        save_contacts(path, contacts)
        print("Contact updated successfully.")
        LOGGER.info("MODIFY | success")

        return 0

    matches = find_matches(contacts, *values)

    if not matches:
        print("Contact not found.")
        LOGGER.warning("REMOVE | contact not found")

        return 1
    
    if len(matches) > 1:
        print("Multiple contacts share this surname; also specify the name.")
        LOGGER.warning("REMOVE | ambiguous surname")

        return 1
    
    if not prompt_confirmation("Delete this contact?"):
        print("Deletion cancelled.")
        LOGGER.info("REMOVE | cancelled")

        return 0

    contacts.remove(matches[0])
    save_contacts(path, contacts)
    print("Contact removed successfully.")
    LOGGER.info("REMOVE | success")

    return 0


@contextmanager
def lock_phone_book(path: Path) -> Iterator[None]:
    """Serialize operations using a persistent sidecar lock file."""
    lock_path = path.with_name(path.name + ".lock")

    with lock_path.open("a+b") as stream:
        if os.name == "nt":
            import msvcrt

            stream.seek(0, os.SEEK_END)

            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()

            stream.seek(0)
            
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise OSError(
                    "contact book is busy; retry when the other operation finishes"
                ) from error
        else:
            import fcntl

            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise OSError(
                    "contact book is busy; retry when the other operation finishes"
                ) from error

        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def main_program(argv=None):
    parser = ArgumentParser(
        description="Manage contacts in a local semicolon-separated CSV contact book.",
        allow_abbrev=False,
        formatter_class=RawDescriptionHelpFormatter,
        epilog="""Examples:
  -a Rossi Alice '+39 333 123 4567' 'Demo contact'
  -m t Rossi Alice '+39 02 123456'
  -m c Rossi Alice 'Updated description'
  -m b Rossi Alice '+39 333 123 4567' 'Updated description'
  -r Rossi [Alice]
  -s Rossi [Alice]
  --list
  --backup
  --import other.csv
  --file contacts.csv --list""",
    )
    parser.add_argument("--file", default=DEFAULT_FILE, metavar="PATH", help="contact book CSV path (default: NoteBook.csv)")
    operations = parser.add_mutually_exclusive_group(required=True)
    operations.add_argument("-a", "--add", nargs="+", help="add a contact")
    operations.add_argument("-m", "--mod", nargs="+", help="modify phone (t), description (c), or both (b)")
    operations.add_argument("-r", "--rem", nargs="+", help="remove contact by surname and optional name")
    operations.add_argument("-s", "--search", nargs="+", help="search by surname and optional name")
    operations.add_argument("--list", action="store_true", help="list all contacts")
    operations.add_argument("--backup", action="store_true", help="create a timestamped backup")
    operations.add_argument("--import", dest="import_file", metavar="CSV", help="import contacts with conflict confirmation")
    args = parser.parse_args(argv)
    operation, values = validate_arguments(parser, args)
    path = Path(args.file).resolve()
    log_path = path.with_suffix(".log")

    if log_path == path or (path.exists() and log_path.exists() and os.path.samefile(path, log_path)):
        parser.error("contact book and log file must have different paths")

    try:
        LOGGER.setLevel(logging.INFO)
        LOGGER.handlers.clear()
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", "%Y-%m-%d %H:%M:%S"))
        LOGGER.addHandler(handler)

        with lock_phone_book(path):
            return run_operation(operation, values, path)
    except (OSError, ContactDataError) as error:
        print(f"Error: {error}", file=sys.stderr)
        LOGGER.error("%s | %s", operation.upper(), type(error).__name__)

        return 1
    except (EOFError, KeyboardInterrupt):
        print("\nOperation cancelled; no changes saved.", file=sys.stderr)
        LOGGER.warning("%s | cancelled", operation.upper())

        return 1
    finally:
        for handler in LOGGER.handlers[:]:
            handler.close()
            LOGGER.removeHandler(handler)


########## MAIN ##########

if __name__ == "__main__":
    sys.exit(main_program())
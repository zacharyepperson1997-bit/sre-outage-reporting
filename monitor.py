import base64
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

STATE_FILE = Path("state.json")
OUTAGES_URL = os.environ.get(
    "SRE_OUTAGES_URL",
    "https://outage.srelectric.com/data/outages.json",
)
THRESHOLD = int(os.environ.get("SRE_THRESHOLD", "250"))
MISSING_POLLS_TO_RESTORE = int(os.environ.get("MISSING_POLLS_TO_RESTORE", "2"))
RESTORED_RETENTION_DAYS = 60


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def parse_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def format_time(value):
    dt = parse_time(value)
    if not dt:
        return value or "Unknown"
    return dt.astimezone().strftime("%b %-d, %Y %I:%M %p %Z")


def duration_text(start_value, end_value=None):
    start = parse_time(start_value)
    end = parse_time(end_value) or datetime.now(timezone.utc)
    if not start:
        return "Unknown"
    seconds = max(0, int((end - start).total_seconds()))
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def load_state():
    if not STATE_FILE.exists():
        return {"active": {}, "restored": {}}
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("state.json must contain a JSON object")
        state.setdefault("active", {})
        state.setdefault("restored", {})
        return state
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f"Could not read state.json safely: {exc}") from exc


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def get_outages():
    request = Request(
        OUTAGES_URL,
        headers={
            "Accept": "application/json",
            "User-Agent": "SRE-Outage-Monitor/2.0",
            "Cache-Control": "no-cache",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not retrieve valid SRE outage data: {exc}") from exc

    if not isinstance(payload, list):
        raise RuntimeError(
            "SRE outages endpoint did not return a JSON array; "
            "refusing to interpret this as all outages being cleared."
        )

    outages = {}
    for index, record in enumerate(payload):
        if not isinstance(record, dict):
            raise RuntimeError(f"Outage record {index} is not a JSON object.")
        outage_id = record.get("outageRecID")
        if not outage_id:
            raise RuntimeError(f"Outage record {index} has no outageRecID.")
        if "customersOutNow" not in record:
            raise RuntimeError(
                f"Outage {outage_id} has no customersOutNow field."
            )
        try:
            record["customersOutNow"] = int(record["customersOutNow"] or 0)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Outage {outage_id} has an invalid customersOutNow value."
            ) from exc
        outages[str(outage_id)] = record

    return outages


def send_sms(body):
    required = (
        "TWILIO_ACCOUNT_SID",
        "TWILIO_AUTH_TOKEN",
        "TWILIO_FROM_NUMBER",
        "TWILIO_TO_NUMBER",
    )
    missing = [key for key in required if not os.environ.get(key)]
    if missing:
        raise RuntimeError(
            "Missing GitHub Actions secrets: " + ", ".join(missing)
        )

    account_sid = os.environ["TWILIO_ACCOUNT_SID"]
    auth_token = os.environ["TWILIO_AUTH_TOKEN"]
    from_number = os.environ["TWILIO_FROM_NUMBER"]
    to_number = os.environ["TWILIO_TO_NUMBER"]

    endpoint = (
        f"https://api.twilio.com/2010-04-01/Accounts/"
        f"{account_sid}/Messages.json"
    )
    data = urlencode({
        "To": to_number,
        "From": from_number,
        "Body": body,
    }).encode("utf-8")

    credentials = base64.b64encode(
        f"{account_sid}:{auth_token}".encode("utf-8")
    ).decode("ascii")

    request = Request(
        endpoint,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )

    try:
        with urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Twilio returned HTTP {exc.code}: {details}"
        ) from exc
    except URLError as exc:
        raise RuntimeError(f"Could not connect to Twilio: {exc}") from exc

    print(
        "Twilio accepted SMS; "
        f"SID={result.get('sid')}, status={result.get('status')}"
    )


def outage_location(record):
    point = record.get("outagePoint") or {}
    lat, lng = point.get("lat"), point.get("lng")
    if lat is not None and lng is not None:
        return f"Location: https://www.google.com/maps?q={lat},{lng}"
    return "Location: see SRE outage map"


def send_new_outage_alert(record):
    outage_id = record["outageRecID"]
    count = record["customersOutNow"]
    start = record.get("outageStartTime")
    initial = record.get("customersOutInitially")
    verified = "Yes" if record.get("verified") else "No"
    crew = "Assigned" if record.get("crewAssigned") else "Not assigned"
    cause = record.get("cause") or "Not reported"

    body = (
        f"SRE NEW OUTAGE: {count:,} meters out "
        f"(threshold >{THRESHOLD}). "
        f"ID: {outage_id}. "
        f"Started: {format_time(start)}. "
        f"Initially affected: {initial if initial is not None else 'Unknown'}. "
        f"Verified: {verified}. Crew: {crew}. Cause: {cause}. "
        f"{outage_location(record)} "
        "SRE map: https://outage.srelectric.com/"
    )
    send_sms(body)


def send_restoration_alert(outage_id, saved, end_time=None):
    start = saved.get("outageStartTime")
    restored_at = end_time or now_iso()
    body = (
        f"SRE OUTAGE CLEARED: {outage_id}. "
        f"This outage previously affected {saved.get('last_customers_out', 'more than 250'):,} meters. "
        f"Started: {format_time(start)}. "
        f"Cleared: {format_time(restored_at)}. "
        f"Duration: {duration_text(start, end_time)}. "
        "SRE map: https://outage.srelectric.com/"
    )
    send_sms(body)


def prune_restored(state):
    cutoff = datetime.now(timezone.utc).timestamp() - (
        RESTORED_RETENTION_DAYS * 24 * 60 * 60
    )
    kept = {}
    for outage_id, item in state["restored"].items():
        restored_at = parse_time(item.get("restored_at"))
        if restored_at and restored_at.timestamp() >= cutoff:
            kept[outage_id] = item
    state["restored"] = kept


def main():
    state = load_state()
    active = state["active"]
    restored = state["restored"]

    # A failed request or malformed response raises an error. It must never
    # be interpreted as an empty map, which could cause false restoration texts.
    outages = get_outages()
    checked_at = now_iso()
    print(f"Successfully retrieved {len(outages)} outage records.")
    print(f"Individual outage threshold: more than {THRESHOLD} meters.")

    # Process all records currently present on the SRE map.
    for outage_id, record in outages.items():
        if outage_id in restored:
            # Prevent a recently cleared record reappearing in a stale response
            # from generating another alert.
            continue

        item = active.setdefault(outage_id, {
            "outageRecID": outage_id,
            "outageStartTime": record.get("outageStartTime"),
            "first_seen_at": checked_at,
            "alerted": False,
            "missing_polls": 0,
        })

        item["outageStartTime"] = (
            record.get("outageStartTime") or item.get("outageStartTime")
        )
        # Reset a prior one-poll absence if the record has returned. Avoid
        # changing timestamps on every poll so GitHub does not commit state
        # unnecessarily every five minutes.
        if item.get("missing_polls", 0):
            item["missing_polls"] = 0
            item.pop("first_missing_at", None)
        if item.get("last_customers_out") != record["customersOutNow"]:
            item["last_customers_out"] = record["customersOutNow"]

        # Alert once per outage ID when it first reaches 251+ meters.
        # This also catches an outage that starts small and later grows.
        if record["customersOutNow"] > THRESHOLD and not item.get("alerted"):
            send_new_outage_alert(record)
            item["alerted"] = True
            item["alerted_at"] = checked_at
            item["last_customers_out"] = record["customersOutNow"]
            print(f"New qualifying outage alert sent: {outage_id}")

        # If SRE explicitly supplies an end time, restoration is confirmed.
        if record.get("outageEndTime"):
            if item.get("alerted"):
                send_restoration_alert(
                    outage_id, item, record.get("outageEndTime")
                )
            active.pop(outage_id, None)
            restored[outage_id] = {"restored_at": checked_at}
            print(f"Outage marked ended by SRE: {outage_id}")

    # An outage absent from a valid response is only declared cleared after
    # two consecutive successful polls, protecting against brief map glitches.
    for outage_id in list(active.keys()):
        if outage_id in outages:
            continue

        item = active[outage_id]
        item["missing_polls"] = int(item.get("missing_polls", 0)) + 1
        item["first_missing_at"] = item.get("first_missing_at") or checked_at

        if item["missing_polls"] >= MISSING_POLLS_TO_RESTORE:
            if item.get("alerted"):
                send_restoration_alert(outage_id, item)
                print(f"Restoration SMS sent: {outage_id}")
            else:
                print(
                    f"Unqualified outage cleared without SMS: {outage_id}"
                )

            active.pop(outage_id, None)
            restored[outage_id] = {"restored_at": checked_at}

    prune_restored(state)
    save_state(state)
    print(
        f"Monitoring complete. Active tracked outages: {len(active)}; "
        f"recently restored IDs retained: {len(restored)}."
    )


if __name__ == "__main__":
    main()

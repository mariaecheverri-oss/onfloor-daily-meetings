import os
from datetime import datetime, time

import pytz
import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

CST = pytz.timezone("America/Chicago")

OWNER_NAMES = ["travis mccutchen", "agustin garcia", "maxwell goldberg"]


def get_env(key):
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(f"Missing environment variable: {key}")
    return val


def hubspot_headers():
    return {
        "Authorization": f"Bearer {get_env('HUBSPOT_TOKEN')}",
        "Content-Type": "application/json",
    }


def fetch_owners():
    owners = {}
    after = None
    while True:
        params = {"limit": 100}
        if after:
            params["after"] = after
        resp = requests.get(
            "https://api.hubapi.com/crm/v3/owners",
            headers=hubspot_headers(),
            params=params,
        )
        resp.raise_for_status()
        data = resp.json()
        for o in data.get("results", []):
            full_name = f"{o.get('firstName', '')} {o.get('lastName', '')}".strip()
            owners[str(o["id"])] = full_name
        paging = data.get("paging", {})
        after = paging.get("next", {}).get("after")
        if not after:
            break
    return owners


def fetch_meetings_today(start_ms, end_ms):
    meetings = []
    after = None
    while True:
        body = {
            "filterGroups": [
                {
                    "filters": [
                        {
                            "propertyName": "hs_meeting_start_time",
                            "operator": "GTE",
                            "value": str(start_ms),
                        },
                        {
                            "propertyName": "hs_meeting_start_time",
                            "operator": "LTE",
                            "value": str(end_ms),
                        },
                    ]
                }
            ],
            "properties": [
                "hs_meeting_title",
                "hs_meeting_start_time",
                "hubspot_owner_id",
            ],
            "limit": 100,
        }
        if after:
            body["after"] = after
        resp = requests.post(
            "https://api.hubapi.com/crm/v3/objects/meetings/search",
            headers=hubspot_headers(),
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()
        meetings.extend(data.get("results", []))
        after = data.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
    return meetings


def fetch_contacts_for_meetings(meeting_ids):
    if not meeting_ids:
        return {}
    resp = requests.post(
        "https://api.hubapi.com/crm/v4/associations/meeting/contact/batch/read",
        headers=hubspot_headers(),
        json={"inputs": [{"id": mid} for mid in meeting_ids]},
    )
    resp.raise_for_status()
    data = resp.json()
    meeting_to_contacts = {}
    for result in data.get("results", []):
        mid = str(result.get("from", {}).get("id", ""))
        contact_ids = [str(a["toObjectId"]) for a in result.get("to", [])]
        if mid and contact_ids:
            meeting_to_contacts[mid] = contact_ids
    return meeting_to_contacts


def fetch_contact_details(contact_ids):
    if not contact_ids:
        return {}
    resp = requests.post(
        "https://api.hubapi.com/crm/v3/objects/contacts/batch/read",
        headers=hubspot_headers(),
        json={
            "inputs": [{"id": cid} for cid in contact_ids],
            "properties": ["firstname", "lastname", "phone", "email", "application_grading"],
        },
    )
    resp.raise_for_status()
    data = resp.json()
    contacts = {}
    for c in data.get("results", []):
        contacts[str(c["id"])] = c.get("properties", {})
    return contacts


def format_phone(phone):
    if not phone:
        return "N/A"
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    if len(digits) == 11 and digits[0] == "1":
        return f"({digits[1:4]}) {digits[4:7]}-{digits[7:]}"
    return phone


@app.route("/trigger", methods=["POST"])
def trigger():
    secret = get_env("TRIGGER_SECRET")
    body = request.get_json(silent=True) or {}
    if body.get("X-Trigger-Secret") != secret:
        return jsonify({"error": "Unauthorized"}), 401

    now_cst = datetime.now(CST)
    if now_cst.weekday() >= 5:
        return jsonify({"message": "Skipping weekend"}), 200

    today_start = CST.localize(datetime.combine(now_cst.date(), time.min))
    today_end = CST.localize(datetime.combine(now_cst.date(), time.max))
    start_ms = int(today_start.timestamp() * 1000)
    end_ms = int(today_end.timestamp() * 1000)

    owners = fetch_owners()
    owner_id_map = {
        oid: name
        for oid, name in owners.items()
        if name.lower() in OWNER_NAMES
    }

    all_meetings = fetch_meetings_today(start_ms, end_ms)

    matching = []
    for m in all_meetings:
        props = m.get("properties", {})
        title = props.get("hs_meeting_title") or ""
        owner_id = str(props.get("hubspot_owner_id") or "")
        if "equipment consultation" in title.lower() and owner_id in owner_id_map:
            matching.append(m)

    date_label = now_cst.strftime("%-m/%-d/%Y").replace(
        now_cst.strftime("%-m/%-d/%Y"),
        now_cst.strftime("%B %-d, %Y"),
    )

    header = f"\U0001f4c5 *Equipment Consultations — {date_label}*"

    if not matching:
        slack_text = f"{header}\n\nNo consultations scheduled for today."
    else:
        meeting_ids = [str(m["id"]) for m in matching]
        meeting_to_contacts = fetch_contacts_for_meetings(meeting_ids)

        all_contact_ids = list({
            cid
            for cids in meeting_to_contacts.values()
            for cid in cids
        })
        contact_details = fetch_contact_details(all_contact_ids)

        matching.sort(
            key=lambda m: int(m["properties"].get("hs_meeting_start_time") or 0)
        )

        blocks = []
        for m in matching:
            props = m.get("properties", {})
            title = props.get("hs_meeting_title") or "Equipment Consultation"
            owner_id = str(props.get("hubspot_owner_id") or "")
            owner_name = owner_id_map.get(owner_id, "Unknown")

            start_ms_val = props.get("hs_meeting_start_time")
            if start_ms_val:
                start_dt = datetime.fromtimestamp(int(start_ms_val) / 1000, tz=CST)
                time_label = start_dt.strftime("%-I:%M %p")
            else:
                time_label = "Unknown time"

            contact_ids = meeting_to_contacts.get(str(m["id"]), [])
            if contact_ids:
                cp = contact_details.get(contact_ids[0], {})
                first = cp.get("firstname") or ""
                last = cp.get("lastname") or ""
                contact_name = f"{first} {last}".strip() or "Unknown"
                phone = format_phone(cp.get("phone"))
                email = cp.get("email") or "N/A"
                grading = cp.get("application_grading") or "N/A"
            else:
                contact_name = "Unknown"
                phone = "N/A"
                email = "N/A"
                grading = "N/A"

            blocks.append(
                f"*{time_label} — {title}*\n"
                f"Owner: {owner_name}\n"
                f"Contact: {contact_name}\n"
                f"Phone: {phone}\n"
                f"Email: {email}\n"
                f"Application Grading: {grading}"
            )

        slack_text = header + "\n\n" + "\n\n".join(blocks)

    slack_resp = requests.post(
        get_env("SLACK_WEBHOOK_URL"),
        json={"text": slack_text},
    )
    slack_resp.raise_for_status()

    return jsonify({"message": "Sent"}), 200


if __name__ == "__main__":
    app.run(debug=True)

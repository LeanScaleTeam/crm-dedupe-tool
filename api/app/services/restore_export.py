"""Build a re-import-ready CSV of the records DELETED by a merge (the losers).

A merge is irreversible — Salesforce deletes the loser records; HubSpot merges are
permanent. This exports the loser records captured pre-merge (merge_backups) in a
CRM-native import format so a *mistaken* merge can be undone by re-importing:

  - Salesforce -> columns are Account/Contact API field names, ready for Data Loader
    "Insert". System/audit + relationship fields are dropped (not insertable); the
    original Id is kept in a reference column only.
  - HubSpot -> columns are property names, ready for "Import -> Create records".

HONEST LIMITATION: re-imported records get NEW ids and do NOT restore relationships
that were re-parented to the winner. This restores the record DATA, not the full
pre-merge graph.
"""
import csv
import io

# Salesforce fields that cannot be set on insert (system/audit/derived).
SF_NONCREATABLE = {
    "Id", "IsDeleted", "MasterRecordId", "CreatedDate", "CreatedById",
    "LastModifiedDate", "LastModifiedById", "SystemModstamp", "LastActivityDate",
    "LastViewedDate", "LastReferencedDate", "PhotoUrl", "JigsawContactId",
    "IsEmailBounced", "EmailBouncedReason", "EmailBouncedDate", "CleanStatus",
    "IndividualId", "attributes", "Name",  # Name is compound/derived on Contact
}

# Original record id, kept for reference only — do NOT map this to Id on insert.
REF_COL = "Merged_From_Id"


def _is_scalar(v) -> bool:
    """Only scalar values become CSV columns; nested relationship/subquery objects
    (e.g. SF Account{}, Opportunities{records:[]}) are skipped — they aren't flat and
    aren't insertable anyway."""
    return v is None or isinstance(v, (str, int, float, bool))


def build_restore_csv(crm_type: str, loser_records: list) -> bytes:
    """One row per deleted (loser) record, CRM-native columns, ready to re-import.

    `loser_records` are the loser_snapshot dicts from merge_backups; their
    raw_properties hold the CRM-native field names/values.
    """
    is_sf = crm_type == "salesforce"

    # Scan all records first: a key is dropped if it is EVER a nested object/list
    # (relationship or subquery), or if it is empty on EVERY record (nothing to
    # restore — this also drops null subqueries like SF Opportunities).
    order: list = []
    seen = set()
    complex_keys = set()
    has_value = set()
    for rec in loser_records:
        src = (rec or {}).get("raw_properties") or {}
        for k, v in src.items():
            if k not in seen:
                seen.add(k)
                order.append(k)
            if isinstance(v, (dict, list)):
                complex_keys.add(k)
            elif v not in (None, ""):
                has_value.add(k)

    keys = [
        k for k in order
        if k not in complex_keys
        and k in has_value
        and not (is_sf and k in SF_NONCREATABLE)
    ]

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([REF_COL] + keys)
    for rec in loser_records:
        src = (rec or {}).get("raw_properties") or {}
        orig_id = (rec or {}).get("id") or src.get("Id") or src.get("hs_object_id") or ""
        row = [orig_id] + ["" if src.get(k) is None else src.get(k) for k in keys]
        writer.writerow(row)

    return buf.getvalue().encode("utf-8")

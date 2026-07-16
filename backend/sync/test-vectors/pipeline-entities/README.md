# Vector: pipeline-entities

Covers the three pipeline entities the older vectors never exercised, and the
two different deletion styles they use:

| entity                 | scenario                                              | outcome |
|------------------------|-------------------------------------------------------|---------|
| `triage`               | shortlisted @10 (A), applied @12 (C), **status='deleted' @14 (A)** | live record with `status="deleted"` — triage deletes are ordinary LWW writes, never tombstones |
| `application_event`    | A logs `interviewing`, C logs `offer` (distinct content-addressed ids) | both live — append-only events coexist, no conflict possible |
| `application_schedule` | A sets dates @11, C **tombstones @13**                | tombstone — engines interpret it as "dates cleared, keep the row" |

The failure this guards against: an engine that models triage deletion as a
tombstone (breaking `no-resurrection` semantics for jobs it shadows), or one
that treats a schedule tombstone as row deletion instead of date clearing.

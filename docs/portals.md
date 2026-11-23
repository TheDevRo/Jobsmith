# Job portals

A **portal** is a small JSON file that tells Jobsmith how to read a job board: a
JSON API, an RSS/Atom feed, or a page carrying schema.org `JobPosting` data.
Jobsmith only *reads* the file. Nothing in it is evaluated, imported or run, and
it can only make requests to the hosts it lists.

Three portals ship with the app (Remotive, Himalayas, Jobicy). All are **off by
default**; turn them on in Settings → Job portals, or in `config.yaml`:

```yaml
search:
  portals:
    remotive: {enabled: true}
```

Add your own by dropping a `.json` file in `<data folder>/portals/`. A file with
a bad format is skipped (the reason is logged); a file can't reuse the id of a
bundled portal or a built-in source.

## Format

```json
{
  "id": "example",
  "name": "Example Jobs",
  "version": "1.0.0",
  "homepage": "https://example.com/",
  "terms_url": "https://example.com/api-terms",
  "free": true,
  "hosts": ["example.com"],
  "kind": "json_api",
  "attribution": "Jobs from Example (example.com).",
  "min_interval_minutes": 60,
  "request": {
    "url": "https://example.com/api/jobs?q={query}&page={page}",
    "headers": {"Authorization": "Bearer {api_key}"},
    "max_pages": 2,
    "max_queries": 3,
    "delay_seconds": 2
  },
  "items": "data.jobs",
  "fields": {
    "external_id": "id",
    "title": "title",
    "company": "company.name",
    "location": "location",
    "url": "apply_url",
    "description": "description_html",
    "posted_at": "published_at"
  },
  "defaults": {"remote": true}
}
```

Unknown keys are rejected at every level.

| Key | Required | Notes |
| --- | --- | --- |
| `id` | yes | 3-32 chars of `a-z 0-9 - _`. Becomes the job's `source`. |
| `name`, `version` | yes | Strings. |
| `homepage`, `terms_url` | yes | `https` URLs. `terms_url` is where the board's API terms live. |
| `free` | yes | `true` or `false`; says whether the API needs payment. |
| `hosts` | yes | 1-5 plain lowercase hostnames (no IPs, wildcards, ports). |
| `kind` | yes | `json_api`, `feed` or `schema_org`. |
| `request.url` | yes | `https`, and its host must be in `hosts`. Placeholders: `{query}` (one request per search keyword, at most 3), `{location}` (first non-"Remote" location), `{page}` (needs `max_pages`). Values are URL-encoded. The host part can't contain a placeholder. |
| `request.headers` | no | Up to 5. Values may use `{api_key}`, filled from `api_keys.<id>_api_key` in config; if no key is set the header is left out. Never put a secret in the file. |
| `request.max_pages` | no | 1-3 (default 1). |
| `request.max_queries` | no | 1-3 (default 3). |
| `request.delay_seconds` | no | 1-30 (default 1) between requests. |
| `items` | `json_api` | Dotted path to the array of jobs (`data.jobs`, `0.items`), or `$` for a top-level array. Not allowed for other kinds. |
| `fields` | `json_api` | Map from a Jobsmith field to a dotted path inside each item. `title` and `url` are required. Lists are joined with `, `. For `feed` it maps to child element names instead (optional; defaults below). Not allowed for `schema_org`. |
| `defaults` | no | `remote` (bool) and `location` (string), used when the item has no value. |
| `attribution` | no | Credit line shown as the source's note in the UI. |
| `min_interval_minutes` | no | Skip a fetch if this portal was fetched more recently. Use it to honour a board's rate limit. |

Job fields: `title`, `company`, `location`, `url`, `description`, `posted_at`,
`salary_min`, `salary_max`, `salary_period`, `salary_currency`, `remote`,
`external_id` (dedupe key; falls back to `url`) and `tags`.

* Salaries: per-month/week figures are converted to annual. When
  `salary_currency` is mapped, anything other than USD is dropped, because the
  salary floor compares raw numbers.
* Jobs without a title or an `http(s)` URL are dropped.

### Kinds

* **`json_api`**: a JSON response; `items` and `fields` as above.
* **`feed`**: RSS `<item>` or Atom `<entry>` elements. Defaults map `title`,
  `link`→`url`, `description` (Atom `summary`), `pubDate` (Atom `updated`)
  →`posted_at`, `author`→`company`, `guid` (Atom `id`)→`external_id`; override
  any with `fields`. Feeds that declare XML entities are refused.
* **`schema_org`**: every `JobPosting` in the page's `application/ld+json`
  blocks, parsed by the same code as manual URL ingestion (`_generic.py`).

## Safety rules the engine enforces

* Only `https`, only hosts in `hosts`, no URL credentials or ports, checked on
  **every redirect hop** (redirects are followed by hand, at most 3).
* Each URL must resolve to a public address (the same guard as `ingest-url`).
* 15 s per request, 45 s per run, 5 MB per response, 200 items, 3 pages.
* A failing portal ends early or returns nothing; it never stops other sources.

## Bundled portals and their terms

| Portal | Terms | What Jobsmith does |
| --- | --- | --- |
| Remotive | [API terms](https://github.com/remotive-com/remote-jobs-api) | Link back and credit Remotive; at most ~4 fetches a day (`min_interval_minutes: 360`), one request per run. |
| Himalayas | [API terms](https://himalayas.app/terms) (section "API Users") | Apply uses Himalayas' `applicationLink` unchanged; credited as the source; fetches at most hourly. Jobs stay on your own device. |
| Jobicy | [API docs and fair use](https://jobicy.com/jobs-rss-feed) | Links to the Jobicy listing; credited; at most hourly, 2 s between requests (limit is 1/s). |

Check a board's current terms before adding it: only bundle or use portals whose
terms allow automated use.

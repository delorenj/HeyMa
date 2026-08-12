# Interchangeable enrichment-pass contract

## Vocabulary

- **Document**: a UTF-8 text artifact presented to an EP. Markdown transcripts
  are documents. A host may normalize HTML, PDF, DOCX, email, or another format
  to text before invoking the EP.
- **Portable core**: content-derived logic with no knowledge of queues,
  databases, object stores, vault locations, or transcript lifecycle state.
- **Runner**: executes the command, validates its result, applies mutations,
  records state, and isolates failures.
- **Host adapter**: translates portable output to runner-specific intents or
  effects. Audio linkage is one such effect; it is not enrichment.

Interchangeability means the same portable core can consume a normalized
ordinary document or a transcript and return the same result shape. It does not
mean every host supports every optional effect.

## Command boundary

Use this CLI shape unless the host already defines a stricter one:

```text
PASS_EXECUTABLE DOCUMENT_PATH [ITEM_ID]
```

- `DOCUMENT_PATH` is the only required input.
- `ITEM_ID` is opaque correlation context. Never use it to fetch required
  content when the supplied path already contains that content.
- Configuration belongs in explicit environment variables or registry fields.
- Exit `0` only after printing a valid result. Use nonzero for failure.
- Diagnostics may go to stderr. The last JSON object on stdout is the result.
- The pass must not edit, rename, move, delete, upload, or tag the document.

## Portable result envelope

HeyMa/Wax currently identifies the envelope as `wax.ep.v1`, but its portable
minimum is document-neutral:

```json
{
  "wax_ep_version": 1,
  "frontmatter": {
    "title": "Portable Document Enrichment",
    "summary": "Defines a reusable enrichment boundary for documents and transcripts.",
    "title-slug": "portable-document-enrichment"
  }
}
```

Rules:

- `wax_ep_version` must be the supported integer version.
- `frontmatter` must be an object, including when empty.
- Values must be JSON-safe and grounded in the supplied document.
- Absence means no proposal. Do not use `null` to erase existing metadata.
- The runner owns merge policy. A pass never assumes its proposal will replace
  a non-empty value.
- Unknown extension keys must either be rejected or ignored safely by the
  runner; do not rely on one without a tested adapter.

Common portable fields include `title`, `summary`, `title-slug`, `tags`,
`topics`, `entities`, `language`, and classifications. Field names and schemas
remain host policy; check the target vault/document schema before choosing
them.

## Ownership and no-clobber

Keep three ownership classes distinct:

| Owner | Examples | EP behavior |
| --- | --- | --- |
| Source/ingest | capture time, source URI, checksum | Never return or modify |
| Human/upstream | an existing title, tags, corrections | Preserve unless explicit `clobber` policy allows replacement |
| This EP | newly derived summary, slug, entities | Propose declaratively; runner applies |

Wax additionally protects its item identity, `wax` state block, capture/source
fields, vault identity, and S3 identity. Read the live protected-field set in
the runner because it may evolve.

## Pure-core pattern

Keep result construction separate from host adaptation:

```python
def enrich_document(text: str, *, item_id: str = "") -> dict[str, object]:
    """Return only content-derived frontmatter proposals."""
    return {"summary": summarize(text)}


def portable_result(text: str, *, item_id: str = "") -> dict[str, object]:
    return {
        "wax_ep_version": 1,
        "frontmatter": enrich_document(text, item_id=item_id),
    }
```

The function may understand transcript syntax when its purpose requires it,
but it must accept a normal document without audio metadata or a ledger row.

## Wax transcript adapter

Wax's current transcript runner recognizes these legacy host extensions:

```json
{
  "transcript": {"slug": "portable-document-enrichment"},
  "link_audio": true
}
```

They request host effects:

- `transcript.slug`: collision-safe transcript rename plus ledger path update;
- `link_audio`: refresh S3 sidecars/tags linking the immutable audio object to
  the current transcript.

Keep them in a thin Wax adapter around the portable result. Do not put archive
credentials, bucket names, transcript table access, or file operations in the
portable core. A metadata-only EP needs no adapter. A generic document host may
map `title-slug` to its own rename intent or ignore it.

If a neutral document-effect extension is introduced later, update the runner,
contract tests, and adapter first; retain backward compatibility for already
registered pass versions.

## Registry baseline

```yaml
slug: example-pass
version: 1
description: "Derive example metadata from any text document"
enabled: false
auto: false
requires: []
clobber: []
timeout_s: 300
command: ["bin/example-pass", "{document_path}", "{item_id}"]
```

Adapt placeholders to the host. Wax uses `{md_path}` and normally anchors the
executable beneath `{component_root}/config/passes.d/bin/`.

## Required test matrix

| Layer | Required proof |
| --- | --- |
| Portable core | Same function succeeds for generic document and transcript fixtures |
| Contract | Final stdout line parses; version and frontmatter types are valid |
| Ownership | Existing values survive unless explicitly clobberable |
| Purity | Input bytes and path are unchanged after direct pass execution |
| Failure | Empty/malformed input and dependency failures exit nonzero without partial mutation |
| Runner | Atomic metadata application and durable per-pass state |
| Adapter | Rename/link effects are collision-safe, idempotent, and absent for generic use |
| Isolation | Failure of one EP does not stop an unrelated EP |
| Versioning | Same version skips when complete; bumped version reruns |


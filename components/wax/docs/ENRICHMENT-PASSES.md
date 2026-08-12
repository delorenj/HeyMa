# Wax enrichment passes

An enrichment pass (EP) is an independently versioned command registered by a
YAML file in `config/passes.d/`. New recordings follow this order:

1. upload and verify the immutable, content-addressed audio object;
2. transcribe and publish the initial Markdown note;
3. run every enabled pass with `auto: true`;
4. re-verify the audio object and park the local source.

A failed pass is recorded but does not gate its siblings or step 4. Automatic
runs skip a pass already completed at the same `version`; increasing `version`
causes the new definition to run once.

## Registry contract

```yaml
slug: title-slug
version: 1
enabled: true
auto: true
requires: []
clobber: []
timeout_s: 600
frontmatter_schema: "{home}/d/_vault/Settings/frontmatter-category-map.json"
env:
  WAX_TITLE_MODEL: qwen3.6:latest
command: ["{component_root}/config/passes.d/bin/title-slug", "{md_path}", "{item_id}"]
```

Supported placeholders are `{component_root}`, `{home}`, `{item_id}`, and
`{md_path}`. `enabled` makes a pass available to `wax ep run`; `auto` also puts
it in the post-transcription flow. Returned values never replace non-empty
frontmatter by default; a pass must explicitly list intentional replacements in
`clobber`.

## Result contract (`wax.ep.v1`)

Commands may log ordinary text, then emit one JSON object as their last output
line. Commands should not edit the note or ledger themselves.

```json
{
  "wax_ep_version": 1,
  "frontmatter": {
    "title": "Modular Transcript Enrichment Passes",
    "summary": "Defines the first automatic enrichment pass and its archive-link policy.",
    "title-slug": "modular-transcript-enrichment-passes"
  },
  "transcript": {
    "slug": "modular-transcript-enrichment-passes"
  },
  "link_audio": true
}
```

Wax applies the vault base schema first, batches the returned frontmatter
through `frontmatters`, performs a collision-safe rename, updates
`transcripts.md_path`, and records pass history in both SQLite and the note's
`wax.passes` map. Provenance keys (`wax-item-id`, source/capture fields, S3
identity, and the `wax` block) are runner-owned and rejected if returned by an
EP.

Legacy commands that emit no `wax_ep_version` object still run and are tracked,
but receive no declarative mutations.

## Audio-to-transcript identity

S3 audio names are never renamed after upload. Object-store “rename” is a
copy-plus-delete operation, which would invalidate the verified key and both
recovery indexes. Instead:

- transcript frontmatter records `source-sha256`, `source-s3-key`, and
  `source-s3-uri`;
- `<audio-key>.wax.json` and `.by-content/<sha256>.json` record the current
  transcript filename, vault-relative path, title, slug, summary, and link time;
- S3 object tags mirror `Transcription`, `ItemId`, `Transcript`, and
  `TitleSlug` without moving audio bytes.

The content ID remains the durable join key even if a person later renames the
note again.

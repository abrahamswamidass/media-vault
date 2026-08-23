# Shared contracts

The two modules never call each other. They meet here, in three document shapes
written to Firestore.

| Collection  | Written by | Read by | Meaning |
|-------------|-----------|---------|---------|
| `items/`    | agent     | web     | A fact. This file exists on the NAS and looks like this. |
| `intents/`  | web       | agent   | A wish. Please do this. The agent writes back `status` + `result`. |
| `actions/`  | agent     | web     | A record. This happened. Append-only mirror of the agent's local journal. |

**One writer per collection.** That rule is the whole design. There is no
merge logic anywhere in this project because no document ever has two authors —
except `intents/`, where the split is by field: web owns `type`/`item_id`/`params`
and never touches `status`/`result`; the agent owns `status`/`result` and never
touches the request.

## Why schemas live here and not in either module

Both sides validate against the same files. When a shape changes, the diff shows
up in one place and both modules break loudly in the same commit, rather than the
web module silently reading a field the agent stopped writing.

These are plain JSON Schema — readable without tooling, and usable with a
validator on either side if you want it. There is deliberately no code generation:
two hand-written type definitions that a reviewer can read beat a build step.

## Files

- `item.schema.json` — a catalogued file
- `intent.schema.json` — a request from the web module
- `action.schema.json` — a completed action

## Content addressing

`item.content_hash` is the join key for everything derived. Thumbnails live at
`thumbs/<hash>.webp` and previews at `previews/<variant>/<hash>.<ext>`, so a blob
is reachable from an item without storing a second pointer, and moving a file on
the NAS invalidates nothing.

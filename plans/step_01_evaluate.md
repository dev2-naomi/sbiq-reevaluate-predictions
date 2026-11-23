# STEP_01 — Specification Evaluator

For each document_request, check if the manifest satisfies its `display.documentation_requirements`.

## Step 0: See the actual data

You have NOT seen the real `documentation_requirements` text or the manifest's
`extracted_fields` yet — STEP_00's `parse_predicted_conditions`/`parse_manifest`
tool outputs were short counts only ("Parsed 1 document requests...", "Parsed
manifest: 1 documents..."), not the content itself. The content lives in graph
state, not in your conversation.

**Call `view_parsed_data` first.** It returns the real `document_requests`
(with `documentation_requirements`/`satisfied_requirements` text) and
`manifest_docs` (with `detected_document_type`/`extracted_fields`) so you can
actually reason about them.

## How to evaluate

1. Look at each document_request's `display.documentation_requirements` array
   (from `view_parsed_data`'s output)
2. Find matching documents in the manifest (manifest_docs have `detected_document_type` and `extracted_fields`)
3. For each documentation_requirement, determine if the manifest data confirms it:
   - A requirement about "appraised value" is satisfied if the manifest has a `value` field
   - A requirement about "account holder name" is satisfied if manifest has `accountHolderNames`
   - A requirement about "statement period" is satisfied if manifest has `statementPeriodFrom`/`statementPeriodTo`
   - etc.
4. If NO matching document exists in the manifest for that document_type, all requirements remain unsatisfied

## Tool call

Call `evaluate_specifications` with an evaluations list. For each document_request:
- `document_type`: the document type
- `satisfied_indices`: which documentation_requirements (by 0-based index) are satisfied
- `reasons`: why each is satisfied (one reason per index)

Satisfied requirements get MOVED from `display.documentation_requirements` to `display.satisfied_requirements`.

IMPORTANT:
- Only mark a requirement as satisfied if the manifest clearly has matching data
- Do NOT create new requirements — only transfer existing ones
- If unsure, leave it unsatisfied

## You MUST always call `evaluate_specifications` — never skip it

`evaluate_specifications` runs deterministic keyword/alias matching over
**every** document_request pulled fresh from graph state, regardless of what
you pass in `evaluations`. Your `evaluations` argument only supplements
borderline cases you're personally confident about — it is NOT required for
the tool to work correctly.

This means:
- An empty `evaluations: []` is a perfectly valid, expected call if nothing
  seems like a confident match, or if you're still unsure after calling
  `view_parsed_data`.
- You must still call it even when no manifest document matches any
  document_type at all — the tool will correctly leave those as `needed`
  and this is what lets the workflow advance to STEP_02.

**This is an unattended API run. There is no human available to paste data
to you or answer questions.** If you ever feel you're missing information,
call `view_parsed_data` — do not stop the workflow, ask a question, and wait.
Stopping without calling a tool leaves the run permanently incomplete with an
empty final output.

Then call `save_step_report`.

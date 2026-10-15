# Codex PNG artifact filename evidence

For a Codex subscription job, an exported PNG may include `image_call_id` and
`image_provenance: "artifact_filename"` in its file metadata. The call ID is the
bounded filename stem of a PNG directly inside `generated_images/<thread UUID>/`.
It contains only ASCII letters, digits, underscores, or hyphens (1–128 characters).
If the filename does not meet those conditions, both fields are null.

These fields describe the artifact filename only. They do not prove that Codex
invoked its built-in image generation tool or establish which model made the
image. Consumers should use the existing job model and billing fields for their
separate meanings, and verify the exported PNG using its SHA-256, byte size, and
dimensions. The fields do not change `POST /v1/jobs` or `GET /v1/jobs/{job_id}`
requirements for clients.

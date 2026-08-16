# Pi TODO

P6 now adds canonical coordination RPC, controlling-TTY approval, one-shot
network operations, immutable lock adapters, deterministic cache-backed npm
and Python materialization, and exact no-cache refusal to the P5
writer-container lifecycle. Later phases still own these boundaries:

- P7 adds durable workstream and broader personal lifecycle behavior.
- P10 adds exhaustive crash, daemon-loss, cancellation, and ambiguous-cleanup
  recovery. Until then, unknown cleanup remains `needs_attention` with the
  durable writer claim retained.
- P12 alone may activate a reviewed generation or touch live Pi paths.

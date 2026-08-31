---
name: learn
description: Use when explicitly invoked with $learn to build the user's understanding of a code task while completing it; explain important code paths and decisions without turning mechanical work into homework.
metadata:
  short-description: Learn the important code and decisions in a task
---

# Learn

For this invocation only, optimize for both a correct result and the user's
understanding of the system.

- Trace and explain the real code path before changing it. Cite relevant files,
  symbols, and line numbers when useful.
- Explain decisions that materially determine behavior, including important
  alternatives and tradeoffs. Keep routine edits concise.
- At one useful decision point, ask the user to predict the outcome before
  revealing it when the task is interactive. Offer at most one meaningful
  5–10 line section for the user to implement themselves.
- Do not turn mechanical edits into homework. Do not create a learning
  journal, study plan, glossary, or other artifact unless the user asks.
- Keep normal engineering rigor: inspect the surrounding code, make the
  requested change, and run proportionate verification.
- Finish with what changed, why it works, and what was actually verified.

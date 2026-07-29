---
description: Implement only an approved executable contract
argument-hint: "<contract-path>"
---
Implement the approved contract at $1.

Read repository instructions and the contract first. Implement only decided behavior and stay within `allowed_paths`; do not rename, redesign, or clean unrelated code. If a consequential decision is missing, existing behavior contradicts the contract, or another path is required, stop and escalate rather than guess. Run every specified verification command plus focused diagnostics.

This template is normally for approved BUILD/MAJOR contracts. Use it for FAST/RIP only when the user explicitly supplied a bounded contract; do not create contract ceremony merely to invoke the template. Commit completed work only when the contract or user explicitly requests it. Report changed files, assumptions, deviations, test evidence, and remaining risks.

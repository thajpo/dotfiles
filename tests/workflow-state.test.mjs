// @ts-check
import { describe, it } from "node:test";
import assert from "node:assert/strict";

// Import the core module (dependency-free)
import {
  PACKET_ENTRY_TYPE,
  PACKET_SCHEMA_VERSION,
  PACKET_CLEAR_TYPE,
  ARTIFACTS_DIR_NAME,
  MODES,
  FAST_REQUIRED,
  RIP_REQUIRED,
  BUILD_REQUIRED,
  MAJOR_REQUIRED,
  SHARED_OPTIONAL,
  MAX_PACKET_BYTES,
  MAX_ARRAY_ITEMS,
  MAX_STRING_CHARS,
  MAX_MANIFEST_SAMPLES,
  validatePacket,
  isValidPacket,
  isValidMode,
  renderCompact,
  parseTaggedPacket,
  latestActivePacket,
  workflowArtifactsDirForSession,
  buildContextManifest,
  boundManifestSamples,
  computeSha256,
  byteLength,
} from "../pi/extensions/workflow-state/core.mjs";

// ============================================================================
// Helpers
// ============================================================================

function makeValidFast() {
  return {
    task_id: "test-001",
    mode: "fast",
    learning: "off",
    goal: "Implement feature X",
    constraints: ["No breaking changes"],
    acceptance: ["Tests pass"],
  };
}

function makeValidRip() {
  return {
    task_id: "rip-001",
    mode: "rip",
    learning: "deep",
    question: "Why is the build slow?",
    environment: "Node 22, Linux",
    useful_evidence: ["Profiler shows GC thrashing"],
    stop_condition: "Build under 5s",
    current_hypotheses: ["GC tuning might help"],
    experiment_results: ["Increased heap reduces pause time"],
  };
}

function makeValidBuild() {
  return {
    task_id: "build-001",
    mode: "build",
    learning: "light",
    intended_behavior: "Users can export to PDF",
    unchanged_behavior: "Existing export formats preserved",
    affected_surfaces: ["ExportService", "/api/export"],
    decisions: ["Use PDFKit"],
    acceptance: ["E2E export test passes"],
  };
}

function makeValidMajor() {
  return {
    task_id: "major-001",
    mode: "major",
    learning: "deep",
    program: {
      desired_end_state: "New auth system deployed",
      work_areas: ["AuthN", "AuthZ", "Migration"],
      dependency_order: ["AuthN -> AuthZ -> Migration"],
      current_slice: "AuthN implementation",
    },
    decisions: ["Use OAuth2"],
    open_decisions: ["Token storage strategy"],
  };
}

function packetEntry(packet, schemaVersion = PACKET_SCHEMA_VERSION) {
  return {
    type: "custom",
    customType: PACKET_ENTRY_TYPE,
    data: { schema_version: schemaVersion, packet },
  };
}

function clearEntry() {
  return { type: "custom", customType: PACKET_CLEAR_TYPE, data: { cleared: true } };
}

/** Full-featured MAJOR packet with all shared fields for lossless render test. */
function makeFullMajor() {
  return {
    task_id: "major-full-001",
    mode: "major",
    learning: "deep",
    original_request: "Rewrite auth system",
    current_interpretation: "OAuth2-based microservice auth",
    goal: "Ship new auth by Q3",
    current_slice: "AuthN implementation",
    boundaries: ["No browser changes"],
    acceptance: ["All E2E tests pass"],
    open_decisions: ["Token storage strategy"],
    decisions: ["Use OAuth2", "Use JWTs"],
    evidence: {
      summaries: ["Bench 1: 2x throughput", "Bench 2: 50% less latency"],
      artifact_paths: ["/tmp/bench-results.json", "/tmp/perf-report.pdf"],
    },
    program: {
      desired_end_state: "New auth system deployed",
      work_areas: ["AuthN", "AuthZ", "Migration"],
      dependency_order: ["AuthN -> AuthZ -> Migration"],
      completed_slices: ["Design doc"],
      current_slice: "AuthN implementation",
      future_slices: ["AuthZ implementation", "Data migration"],
    },
    candidates: ["OAuth2", "SAML", "OpenID Connect"],
    remaining_uncertainty: ["Performance under 10K concurrent users"],
  };
}

// ============================================================================
// Tests
// ============================================================================

describe("workflow-state core", () => {
  // --------------------------------------------------------------------------
  // Constants
  // --------------------------------------------------------------------------
  describe("constants", () => {
    it("MODES contains all valid modes", () => {
      assert.deepEqual([...MODES].sort(), ["build", "fast", "major", "rip"]);
    });

    it("FAST_REQUIRED has exactly 6 fields", () => {
      assert.equal(FAST_REQUIRED.length, 6);
      assert.ok(FAST_REQUIRED.includes("task_id"));
      assert.ok(FAST_REQUIRED.includes("mode"));
      assert.ok(FAST_REQUIRED.includes("learning"));
      assert.ok(FAST_REQUIRED.includes("goal"));
      assert.ok(FAST_REQUIRED.includes("constraints"));
      assert.ok(FAST_REQUIRED.includes("acceptance"));
    });

    it("MAJOR_REQUIRED includes nested program fields", () => {
      assert.ok(MAJOR_REQUIRED.includes("program.desired_end_state"));
      assert.ok(MAJOR_REQUIRED.includes("program.work_areas"));
      assert.ok(MAJOR_REQUIRED.includes("program.dependency_order"));
      assert.ok(MAJOR_REQUIRED.includes("program.current_slice"));
    });

    it("packet and manifest bounds are conservative", () => {
      assert.equal(MAX_PACKET_BYTES, 32 * 1024);
      assert.equal(MAX_ARRAY_ITEMS, 100);
      assert.equal(MAX_STRING_CHARS, 8_000);
      assert.equal(MAX_MANIFEST_SAMPLES, 100);
    });

    it("entry type constants are distinct and versioned", () => {
      assert.equal(PACKET_SCHEMA_VERSION, 1);
      assert.notEqual(PACKET_ENTRY_TYPE, PACKET_CLEAR_TYPE);
      assert.ok(PACKET_ENTRY_TYPE.startsWith("workflow-"));
      assert.ok(PACKET_CLEAR_TYPE.startsWith("workflow-"));
    });

    it("ARTIFACTS_DIR_NAME is workflow-artifacts", () => {
      assert.equal(ARTIFACTS_DIR_NAME, "workflow-artifacts");
    });
  });

  // --------------------------------------------------------------------------
  // isValidMode
  // --------------------------------------------------------------------------
  describe("isValidMode", () => {
    it("returns true for valid modes", () => {
      for (const mode of MODES) {
        assert.equal(isValidMode(mode), true);
      }
    });

    it("returns false for invalid modes", () => {
      assert.equal(isValidMode("invalid"), false);
      assert.equal(isValidMode(""), false);
      assert.equal(isValidMode("FAST"), false);
    });
  });

  // --------------------------------------------------------------------------
  // validatePacket - FAST mode
  // --------------------------------------------------------------------------
  describe("validatePacket - FAST mode", () => {
    it("accepts a valid FAST packet", () => {
      const errors = validatePacket(makeValidFast());
      assert.deepEqual(errors, []);
      assert.equal(isValidPacket(makeValidFast()), true);
    });

    it("rejects FAST packet with missing required field", () => {
      const packet = makeValidFast();
      delete packet.goal;
      const errors = validatePacket(packet);
      assert.ok(errors.length > 0);
      assert.ok(errors.some((e) => e.startsWith("goal")));
    });

    it("rejects FAST packet with extra field", () => {
      const packet = { ...makeValidFast(), extra_field: "not allowed" };
      const errors = validatePacket(packet);
      assert.ok(errors.some((e) => e.startsWith("extra_field")));
    });

    it("rejects FAST packet with all extra shared fields", () => {
      const packet = {
        ...makeValidFast(),
        original_request: "test",
        boundaries: "test",
        candidates: "test",
      };
      const errors = validatePacket(packet);
      assert.ok(errors.some((e) => e.startsWith("original_request")));
      assert.ok(errors.some((e) => e.startsWith("boundaries")));
      assert.ok(errors.some((e) => e.startsWith("candidates")));
    });

    it("rejects invalid mode", () => {
      const errors = validatePacket({ ...makeValidFast(), mode: "turbo" });
      assert.ok(errors.some((e) => e.startsWith("mode")));
    });

    it("rejects invalid learning value", () => {
      const errors = validatePacket({ ...makeValidFast(), learning: "extreme" });
      assert.ok(errors.some((e) => e.startsWith("learning")));
    });

    it("rejects scalar constraints and empty acceptance", () => {
      const scalar = validatePacket({ ...makeValidFast(), constraints: "not-an-array" });
      assert.ok(scalar.some((e) => e.startsWith("constraints")));
      const empty = validatePacket({ ...makeValidFast(), acceptance: [] });
      assert.ok(empty.some((e) => e.startsWith("acceptance")));
    });

    it("rejects empty task_id", () => {
      const errors = validatePacket({ ...makeValidFast(), task_id: "" });
      assert.ok(errors.some((e) => e.startsWith("task_id")));
    });

    it("rejects non-object packet", () => {
      assert.deepEqual(validatePacket(null), ["packet must be a non-null object"]);
      assert.deepEqual(validatePacket("string"), ["packet must be a non-null object"]);
      assert.deepEqual(validatePacket([]), ["packet must be a non-null object"]);
    });
  });

  // --------------------------------------------------------------------------
  // validatePacket - RIP mode
  // --------------------------------------------------------------------------
  describe("validatePacket - RIP mode", () => {
    it("accepts a valid RIP packet", () => {
      assert.deepEqual(validatePacket(makeValidRip()), []);
    });

    it("accepts RIP packet with shared optional fields", () => {
      const packet = {
        ...makeValidRip(),
        original_request: "Investigate build perf",
        goal: "Speed up builds",
        boundaries: ["CI only"],
        remaining_uncertainty: ["Cache invalidation strategy"],
      };
      assert.deepEqual(validatePacket(packet), []);
    });

    it("rejects RIP with missing required field", () => {
      const packet = makeValidRip();
      delete packet.question;
      const errors = validatePacket(packet);
      assert.ok(errors.some((e) => e.startsWith("question")));
    });

    it("accepts an initial RIP packet without hypotheses or results", () => {
      const packet = makeValidRip();
      delete packet.current_hypotheses;
      delete packet.experiment_results;
      assert.deepEqual(validatePacket(packet), []);
    });
  });

  // --------------------------------------------------------------------------
  // validatePacket - BUILD mode
  // --------------------------------------------------------------------------
  describe("validatePacket - BUILD mode", () => {
    it("accepts a valid BUILD packet", () => {
      assert.deepEqual(validatePacket(makeValidBuild()), []);
    });

    it("accepts BUILD with shared optional fields", () => {
      const packet = {
        ...makeValidBuild(),
        open_decisions: ["UI framework choice"],
        evidence: { summaries: ["Benchmark results"], artifact_paths: ["/tmp/bench.json"] },
      };
      assert.deepEqual(validatePacket(packet), []);
    });

    it("rejects BUILD with missing acceptance", () => {
      const packet = makeValidBuild();
      delete packet.acceptance;
      const errors = validatePacket(packet);
      assert.ok(errors.some((e) => e.startsWith("acceptance") && e.includes("required")));
    });

    it("rejects malformed evidence", () => {
      const errors = validatePacket({ ...makeValidBuild(), evidence: { summaries: "bad" } });
      assert.ok(errors.some((e) => e.startsWith("evidence.summaries")));
      assert.ok(errors.some((e) => e.startsWith("evidence.artifact_paths")));
    });

    it("rejects unknown and oversized fields", () => {
      const unknown = validatePacket({ ...makeValidBuild(), raw_transcript: "secret" });
      assert.ok(unknown.some((e) => e.startsWith("raw_transcript")));
      const oversized = validatePacket({ ...makeValidBuild(), current_interpretation: "x".repeat(MAX_PACKET_BYTES) });
      assert.ok(oversized.some((e) => e.startsWith("packet")));
      assert.ok(oversized.some((e) => e.startsWith("current_interpretation")));
    });

    it("rejects non-string and excessive array items", () => {
      const wrongType = validatePacket({ ...makeValidBuild(), decisions: ["ok", { hidden: true }] });
      assert.ok(wrongType.some((e) => e.startsWith("decisions[1]")));
      const excessive = validatePacket({ ...makeValidBuild(), decisions: Array(MAX_ARRAY_ITEMS + 1).fill("x") });
      assert.ok(excessive.some((e) => e.startsWith("decisions")));
    });
  });

  // --------------------------------------------------------------------------
  // validatePacket - MAJOR mode
  // --------------------------------------------------------------------------
  describe("validatePacket - MAJOR mode", () => {
    it("accepts a valid MAJOR packet", () => {
      assert.deepEqual(validatePacket(makeValidMajor()), []);
    });

    it("rejects MAJOR with missing program.desired_end_state", () => {
      const packet = makeValidMajor();
      delete packet.program.desired_end_state;
      const errors = validatePacket(packet);
      assert.ok(errors.some((e) => e.startsWith("program.desired_end_state")));
    });

    it("rejects MAJOR with missing program.work_areas", () => {
      const packet = makeValidMajor();
      delete packet.program.work_areas;
      const errors = validatePacket(packet);
      assert.ok(errors.some((e) => e.startsWith("program.work_areas")));
    });

    it("rejects MAJOR with missing program entirely", () => {
      const packet = makeValidMajor();
      delete packet.program;
      const errors = validatePacket(packet);
      assert.ok(errors.some((e) => e.startsWith("program.desired_end_state")));
    });

    it("rejects MAJOR with missing decisions", () => {
      const packet = makeValidMajor();
      delete packet.decisions;
      const errors = validatePacket(packet);
      assert.ok(errors.some((e) => e.startsWith("decisions")));
    });

    it("rejects MAJOR with missing open_decisions", () => {
      const packet = makeValidMajor();
      delete packet.open_decisions;
      const errors = validatePacket(packet);
      assert.ok(errors.some((e) => e.startsWith("open_decisions")));
    });

    it("accepts MAJOR with shared optional fields", () => {
      const packet = {
        ...makeValidMajor(),
        boundaries: ["No browser changes"],
        candidates: ["OAuth2", "SAML"],
        remaining_uncertainty: ["Token storage approach"],
      };
      assert.deepEqual(validatePacket(packet), []);
    });

    it("rejects empty work areas and malformed program arrays", () => {
      const empty = makeValidMajor();
      empty.program.work_areas = [];
      assert.ok(validatePacket(empty).some((e) => e.startsWith("program.work_areas")));
      const malformed = makeValidMajor();
      malformed.program.dependency_order = "not-an-array";
      assert.ok(validatePacket(malformed).some((e) => e.startsWith("program.dependency_order")));
    });

    it("rejects unknown nested program and candidate fields", () => {
      const badProgram = makeValidMajor();
      badProgram.program.raw_history = "not allowed";
      assert.ok(validatePacket(badProgram).some((e) => e.startsWith("program.raw_history")));
      const badCandidate = { ...makeValidMajor(), candidates: [{ candidate_id: "a", transcript: "not allowed" }] };
      assert.ok(validatePacket(badCandidate).some((e) => e.includes("transcript")));
    });
  });

  // --------------------------------------------------------------------------
  // renderCompact / parseTaggedPacket — lossless tagged JSON
  // --------------------------------------------------------------------------
  describe("renderCompact (lossless tagged JSON)", () => {
    it("round-trips a FAST packet", () => {
      const original = makeValidFast();
      const text = renderCompact(original);
      assert.ok(text.startsWith("TASK_PACKET "));
      const parsed = parseTaggedPacket(text);
      assert.deepEqual(parsed, original);
    });

    it("round-trips a full MAJOR packet with nested arrays and objects", () => {
      const original = makeFullMajor();
      const text = renderCompact(original);
      const parsed = parseTaggedPacket(text);
      assert.deepEqual(parsed, original);
    });

    it("round-trips a BUILD packet with evidence object", () => {
      const original = {
        ...makeValidBuild(),
        evidence: { summaries: ["test"], artifact_paths: ["path"] },
      };
      const text = renderCompact(original);
      const parsed = parseTaggedPacket(text);
      assert.deepEqual(parsed, original);
    });

    it("returns empty string for null/undefined", () => {
      assert.equal(renderCompact(null), "");
      assert.equal(renderCompact(undefined), "");
    });

    it("parseTaggedPacket returns null for invalid input", () => {
      assert.equal(parseTaggedPacket("not json"), null);
      assert.equal(parseTaggedPacket(null), null);
      assert.equal(parseTaggedPacket("TASK_PACKET not json"), null);
    });

    it("parseTaggedPacket accepts bare JSON without prefix", () => {
      const parsed = parseTaggedPacket('{"task_id":"t1","mode":"fast","learning":"off","goal":"G","constraints":"C","acceptance":"A"}');
      assert.notEqual(parsed, null);
      assert.equal(parsed?.task_id, "t1");
    });
  });

  // --------------------------------------------------------------------------
  // latestActivePacket - replace/clear reconstruction (branch-correct)
  // --------------------------------------------------------------------------
  describe("latestActivePacket", () => {
    it("returns null for empty entries", () => {
      assert.equal(latestActivePacket([]), null);
    });

    it("returns null for null entries", () => {
      assert.equal(latestActivePacket(null), null);
    });

    it("returns the latest packet when no clear exists", () => {
      const entries = [
        packetEntry({ ...makeValidFast(), task_id: "first" }),
        packetEntry({ ...makeValidBuild(), task_id: "second" }),
      ];
      assert.equal(latestActivePacket(entries)?.task_id, "second");
    });

    it("returns a versioned packet entry", () => {
      const packet = makeValidFast();
      assert.deepEqual(latestActivePacket([packetEntry(packet)]), packet);
    });

    it("returns null after clear (tombstone after last packet)", () => {
      assert.equal(latestActivePacket([packetEntry(makeValidFast()), clearEntry()]), null);
    });

    it("returns latest packet if clear is before a newer packet", () => {
      const entries = [packetEntry(makeValidFast()), clearEntry(), packetEntry(makeValidBuild())];
      assert.equal(latestActivePacket(entries)?.task_id, "build-001");
    });

    it("fails closed on an unversioned or unsupported newest entry", () => {
      const older = packetEntry({ ...makeValidFast(), task_id: "older" });
      const unversioned = { type: "custom", customType: PACKET_ENTRY_TYPE, data: makeValidBuild() };
      assert.equal(latestActivePacket([older, unversioned]), null);
      assert.equal(latestActivePacket([older, packetEntry(makeValidBuild(), 999)]), null);
    });

    it("fails closed on a malformed or invalid newest envelope", () => {
      const older = packetEntry({ ...makeValidFast(), task_id: "older" });
      const malformed = { type: "custom", customType: PACKET_ENTRY_TYPE, data: { schema_version: 1 } };
      assert.equal(latestActivePacket([older, malformed]), null);
      const invalid = packetEntry({ ...makeValidBuild(), raw_transcript: "not allowed" });
      assert.equal(latestActivePacket([older, invalid]), null);
    });

    it("ignores non-custom entries", () => {
      const entries = [
        { type: "message", message: { role: "user", content: "hi" } },
        packetEntry({ ...makeValidFast(), task_id: "found" }),
      ];
      assert.equal(latestActivePacket(entries)?.task_id, "found");
    });

    it("returns null when there is no packet entry at all", () => {
      const entries = [
        { type: "message", message: { role: "user", content: "hi" } },
        { type: "custom", customType: "other-type", data: { foo: "bar" } },
      ];
      assert.equal(latestActivePacket(entries), null);
    });

    it("works with getBranch-shaped input (custom objects with type/customType/data)", () => {
      assert.equal(latestActivePacket([packetEntry(makeValidBuild())])?.task_id, "build-001");
    });
  });

  // --------------------------------------------------------------------------
  // workflowArtifactsDirForSession — real path helper
  // --------------------------------------------------------------------------
  describe("workflowArtifactsDirForSession", () => {
    it("resolves to session-scoped workflow-artifacts directory", () => {
      const result = workflowArtifactsDirForSession("/home/user/.pi/agent/sessions/abc123.jsonl");
      assert.equal(
        result,
        "/home/user/.pi/agent/sessions/abc123/workflow-artifacts",
      );
    });

    it("never contains cwd/project paths", () => {
      const result = workflowArtifactsDirForSession("/home/user/.pi/agent/sessions/sess-001.jsonl");
      assert.ok(result.startsWith("/home/user/.pi/agent/sessions/sess-001/"));
      assert.ok(!result.includes(process.cwd()));
    });

    it("handles session file in tmp dir", () => {
      const result = workflowArtifactsDirForSession("/tmp/pi-test-session/sess.jsonl");
      assert.equal(result, "/tmp/pi-test-session/sess/workflow-artifacts");
    });
  });

  // --------------------------------------------------------------------------
  // Context manifest
  // --------------------------------------------------------------------------
  describe("buildContextManifest", () => {
    it("produces a valid manifest structure", async () => {
      const manifest = await buildContextManifest({
        generationId: "test-1",
        systemPrompt: "You are a helpful assistant.",
        messages: [{ role: "user", content: "Hello" }],
        sessionId: "sess-001",
        includeRaw: false,
      });

      assert.equal(manifest.manifest_version, 1);
      assert.equal(manifest.generation_id, "test-1");
      assert.equal(manifest.session_id, "sess-001");
      assert.equal(manifest.is_child, false);
      assert.equal(manifest.child_agent, null);
      assert.equal(manifest.child_index, null);
      assert.ok(typeof manifest.captured_at === "number");
      assert.ok(manifest.system_prompt);
      assert.equal(manifest.system_prompt.chars, 28);
      assert.ok(typeof manifest.system_prompt.hash === "string");
      assert.equal(manifest.system_prompt.hash.length, 64);
      assert.equal(manifest.system_prompt.text, undefined); // not included without raw
      assert.ok(Array.isArray(manifest.messages));
      assert.equal(manifest.messages.length, 1);
      assert.equal(manifest.messages[0].role, "user");
      assert.ok(typeof manifest.messages[0].hash === "string");
      assert.equal(manifest.messages[0].content, undefined); // not included without raw
      // session_file, model, thinking_level, context_usage, context_files, selected_tools, skill_names should be null when not provided
      assert.equal(manifest.session_file, null);
      assert.equal(manifest.model, null);
      assert.equal(manifest.thinking_level, null);
      assert.equal(manifest.context_usage, null);
      assert.equal(manifest.context_files, null);
      assert.equal(manifest.selected_tools, null);
      assert.equal(manifest.skill_names, null);
    });

    it("includes raw content when includeRaw is true", async () => {
      const manifest = await buildContextManifest({
        generationId: "test-raw",
        systemPrompt: "Be brief.",
        messages: [{ role: "system", content: "You are a bot" }],
        includeRaw: true,
      });

      assert.equal(manifest.system_prompt.text, "Be brief.");
      assert.equal(manifest.messages[0].content, "You are a bot");
    });

    it("includes context_usage with all three fields", async () => {
      const manifest = await buildContextManifest({
        generationId: "test-cu",
        systemPrompt: "",
        messages: [],
        contextUsagePercent: 45.2,
        contextTokens: 12345,
        contextWindow: 128000,
      });

      assert.deepEqual(manifest.context_usage, {
        tokens: 12345,
        context_window: 128000,
        percent: 45.2,
      });
    });

    it("includes partial context_usage with nulls for missing fields", async () => {
      const manifest = await buildContextManifest({
        generationId: "test-cu-partial",
        systemPrompt: "",
        messages: [],
        contextUsagePercent: 50,
        // tokens and window not provided
      });

      assert.deepEqual(manifest.context_usage, {
        tokens: null,
        context_window: null,
        percent: 50,
      });
    });

    it("includes context_usage null when no fields provided", async () => {
      const manifest = await buildContextManifest({
        generationId: "test-cu-none",
        systemPrompt: "",
        messages: [],
      });

      assert.equal(manifest.context_usage, null);
    });

    it("includes task_packet metadata", async () => {
      const packetJson = JSON.stringify({ task_id: "t1", mode: "fast", learning: "off", goal: "G", constraints: ["C"], acceptance: ["A"] });
      const manifest = await buildContextManifest({
        generationId: "test-tp",
        systemPrompt: "",
        messages: [],
        taskPacketJson: packetJson,
        taskPacketSize: new TextEncoder().encode(packetJson).length,
      });

      assert.equal(manifest.task_packet.present, true);
      assert.ok(manifest.task_packet.size > 0);
    });

    it("includes thinking_level from ctx.thinkingLevel", async () => {
      const manifest = await buildContextManifest({
        generationId: "test-tl",
        systemPrompt: "",
        messages: [],
        thinkingLevel: "high",
      });
      assert.equal(manifest.thinking_level, "high");
    });

    it("includes session_file as storage path (no content hash)", async () => {
      const manifest = await buildContextManifest({
        generationId: "test-sf",
        systemPrompt: "",
        messages: [],
        sessionFile: "/home/user/.pi/agent/sessions/abc123.jsonl",
      });
      assert.equal(manifest.session_file, "/home/user/.pi/agent/sessions/abc123.jsonl");
    });

    it("includes child_agent and child_index when provided", async () => {
      const manifest = await buildContextManifest({
        generationId: "test-ci",
        systemPrompt: "",
        messages: [],
        isChild: true,
        childAgent: "worker",
        childIndex: 0,
      });
      assert.equal(manifest.is_child, true);
      assert.equal(manifest.child_agent, "worker");
      assert.equal(manifest.child_index, 0);
    });

    it("includes selected_tools and skill_names when provided", async () => {
      const manifest = await buildContextManifest({
        generationId: "test-ts",
        systemPrompt: "",
        messages: [],
        selectedTools: ["read", "bash", "edit"],
        skillNames: ["typescript", "node"],
      });
      assert.deepEqual(manifest.selected_tools, ["read", "bash", "edit"]);
      assert.deepEqual(manifest.skill_names, ["typescript", "node"]);
    });

    it("includes context_files array with path/bytes/chars/hash", async () => {
      const manifest = await buildContextManifest({
        generationId: "test-cfs",
        systemPrompt: "",
        messages: [],
        contextFiles: [
          { path: "/home/user/.pi/ctx/context.md", content: "Some context content" },
          { path: "/home/user/.pi/ctx/rules.json", content: '{"key":"value"}' },
        ],
        includeRaw: false,
      });

      assert.ok(Array.isArray(manifest.context_files));
      assert.equal(manifest.context_files.length, 2);
      assert.equal(manifest.context_files[0].path, "/home/user/.pi/ctx/context.md");
      assert.equal(manifest.context_files[0].chars, 20);
      assert.ok(typeof manifest.context_files[0].hash === "string");
      assert.equal(manifest.context_files[0].hash.length, 64);
      assert.equal(manifest.context_files[0].content, undefined); // not raw
      assert.equal(manifest.context_files[1].path, "/home/user/.pi/ctx/rules.json");
      assert.equal(manifest.context_files[1].content, undefined); // not raw
    });

    it("includes raw context file content when includeRaw is true", async () => {
      const manifest = await buildContextManifest({
        generationId: "test-cfs-raw",
        systemPrompt: "",
        messages: [],
        contextFiles: [
          { path: "/tmp/ctx.md", content: "raw content" },
        ],
        includeRaw: true,
      });
      assert.equal(manifest.context_files[0].content, "raw content");
    });

    it("includes submitted_prompt when provided", async () => {
      const manifest = await buildContextManifest({
        generationId: "test-sp",
        systemPrompt: "system",
        messages: [],
        submittedPrompt: "Implement feature X",
        includeRaw: false,
      });

      assert.ok(manifest.submitted_prompt);
      assert.equal(manifest.submitted_prompt.chars, 19);
      assert.equal(manifest.submitted_prompt.bytes, 19);
      assert.ok(typeof manifest.submitted_prompt.hash === "string");
      assert.equal(manifest.submitted_prompt.hash.length, 64);
      assert.equal(manifest.submitted_prompt.text, undefined); // not raw
    });

    it("includes raw submitted_prompt text when includeRaw is true", async () => {
      const manifest = await buildContextManifest({
        generationId: "test-sp-raw",
        systemPrompt: "system",
        messages: [],
        submittedPrompt: "Fix the bug",
        includeRaw: true,
      });
      assert.equal(manifest.submitted_prompt.text, "Fix the bug");
    });

    it("omits submitted_prompt when not provided", async () => {
      const manifest = await buildContextManifest({
        generationId: "test-sp-none",
        systemPrompt: "system",
        messages: [],
      });
      assert.equal(manifest.submitted_prompt, undefined);
    });

    it("includes skill_names when provided via options", async () => {
      const manifest = await buildContextManifest({
        generationId: "test-sk",
        systemPrompt: "system",
        messages: [],
        skillNames: ["typescript", "node", "python"],
      });
      assert.deepEqual(manifest.skill_names, ["typescript", "node", "python"]);
    });

    it("includes all combined fields (submitted_prompt + skills + tools + files)", async () => {
      const manifest = await buildContextManifest({
        generationId: "test-combined",
        systemPrompt: "system",
        messages: [{ role: "user", content: "hello" }],
        submittedPrompt: "Do the thing",
        skillNames: ["node"],
        selectedTools: ["read", "bash"],
        contextFiles: [{ path: "/tmp/rules.md", content: "rules content" }],
      });
      assert.equal(manifest.submitted_prompt.chars, 12);
      assert.deepEqual(manifest.skill_names, ["node"]);
      assert.deepEqual(manifest.selected_tools, ["read", "bash"]);
      assert.equal(manifest.context_files.length, 1);
      assert.equal(manifest.context_files[0].path, "/tmp/rules.md");
    });
  });

  // --------------------------------------------------------------------------
  // boundManifestSamples
  // --------------------------------------------------------------------------
  describe("boundManifestSamples", () => {
    it("returns same array when under limit", () => {
      const samples = Array.from({ length: 50 }, (_, i) => ({ id: i }));
      const result = boundManifestSamples(samples);
      assert.equal(result.length, 50);
    });

    it("trims to MAX_MANIFEST_SAMPLES when over limit", () => {
      const samples = Array.from({ length: 150 }, (_, i) => ({ id: i }));
      const result = boundManifestSamples(samples);
      assert.equal(result.length, MAX_MANIFEST_SAMPLES);
      assert.equal(result[0].id, 50); // oldest dropped
    });

    it("returns empty array for null/undefined", () => {
      assert.deepEqual(boundManifestSamples(null), []);
      assert.deepEqual(boundManifestSamples(undefined), []);
    });
  });

  // --------------------------------------------------------------------------
  // computeSha256
  // --------------------------------------------------------------------------
  describe("computeSha256", () => {
    it("produces a 64-char hex string", async () => {
      const hash = await computeSha256("hello");
      assert.equal(hash.length, 64);
      assert.ok(/^[a-f0-9]{64}$/.test(hash));
    });

    it("is deterministic", async () => {
      const a = await computeSha256("test data");
      const b = await computeSha256("test data");
      assert.equal(a, b);
    });

    it("differs for different inputs", async () => {
      const a = await computeSha256("hello");
      const b = await computeSha256("world");
      assert.notEqual(a, b);
    });
  });

  // --------------------------------------------------------------------------
  // byteLength
  // --------------------------------------------------------------------------
  describe("byteLength", () => {
    it("returns byte length (ASCII)", () => {
      assert.equal(byteLength("hello"), 5);
    });

    it("returns byte length (multi-byte)", () => {
      assert.equal(byteLength("héllo"), 6);
      assert.equal(byteLength("你好"), 6);
    });
  });

  // --------------------------------------------------------------------------
  // Integration: replace + clear sequence
  // --------------------------------------------------------------------------
  describe("replace/clear integration", () => {
    it("full lifecycle: null -> packet -> cleared -> null", () => {
      const entries1 = [];
      assert.equal(latestActivePacket(entries1), null);

      const entries2 = [packetEntry(makeValidFast())];
      assert.notEqual(latestActivePacket(entries2), null);

      const entries3 = [packetEntry(makeValidFast()), clearEntry()];
      assert.equal(latestActivePacket(entries3), null);

      const entries4 = [packetEntry(makeValidFast()), clearEntry(), packetEntry(makeValidBuild())];
      assert.notEqual(latestActivePacket(entries4), null);
      assert.equal(latestActivePacket(entries4)?.mode, "build");
    });
  });

  // --------------------------------------------------------------------------
  // Multi-packet history (old packets stay as non-context custom entries)
  // --------------------------------------------------------------------------
  describe("old packets remain as non-context custom entries", () => {
    it("only the latest active entry is reconstructed; older ones are not lost", () => {
      const entries = [
        { type: "custom", customType: "other-data", data: { unrelated: true } },
        packetEntry({ ...makeValidFast(), task_id: "old" }),
        packetEntry({ ...makeValidBuild(), task_id: "latest" }),
      ];
      const packet = latestActivePacket(entries);
      assert.equal(packet?.task_id, "latest");
      // The entries array still has both packets
      assert.equal(entries.length, 3);
    });
  });

  // --------------------------------------------------------------------------
  // workflowArtifactsDirForSession — edge cases
  // --------------------------------------------------------------------------
  describe("workflowArtifactsDirForSession edge cases", () => {
    it("handles session file in root dir", () => {
      const result = workflowArtifactsDirForSession("/abc.jsonl");
      assert.equal(result, "/abc/workflow-artifacts");
    });
  });
});

// ============================================================================
// Context audit opt-in tests
// ============================================================================
describe("workflow-state context audit opt-in", () => {
  it("produces manifests without raw content when includeRaw=false", async () => {
    const manifest = await buildContextManifest({
      generationId: "opt-in-test",
      systemPrompt: "System prompt",
      messages: [{ role: "user", content: "User message" }],
      includeRaw: false,
    });
    assert.equal(manifest.system_prompt.text, undefined);
    assert.equal(manifest.messages[0].content, undefined);
  });

  it("produces manifests with raw content when includeRaw=true", async () => {
    const manifest = await buildContextManifest({
      generationId: "opt-in-raw-test",
      systemPrompt: "System prompt",
      messages: [{ role: "user", content: "User message" }],
      includeRaw: true,
    });
    assert.equal(manifest.system_prompt.text, "System prompt");
    assert.equal(manifest.messages[0].content, "User message");
  });

  it("includes all context fields when provided (child, tools, skills, files)", async () => {
    const manifest = await buildContextManifest({
      generationId: "opt-in-full",
      systemPrompt: "System prompt",
      messages: [{ role: "user", content: "hello" }],
      isChild: true,
      childAgent: "scout",
      childIndex: 2,
      selectedTools: ["read", "grep"],
      skillNames: ["typescript"],
      thinkingLevel: "medium",
      contextFiles: [{ path: "/tmp/ctx.md", content: "loaded context" }],
    });
    assert.equal(manifest.is_child, true);
    assert.equal(manifest.child_agent, "scout");
    assert.equal(manifest.child_index, 2);
    assert.deepEqual(manifest.selected_tools, ["read", "grep"]);
    assert.deepEqual(manifest.skill_names, ["typescript"]);
    assert.equal(manifest.thinking_level, "medium");
    assert.ok(Array.isArray(manifest.context_files));
    assert.equal(manifest.context_files.length, 1);
    assert.equal(manifest.context_files[0].path, "/tmp/ctx.md");
  });
});

// ============================================================================
// Bounded manifest samples test
// ============================================================================
describe("bounded manifest samples", () => {
  it("drops oldest samples when over MAX_MANIFEST_SAMPLES", () => {
    const count = MAX_MANIFEST_SAMPLES + 20;
    const samples = Array.from({ length: count }, (_, i) => ({ index: i }));
    const bounded = boundManifestSamples(samples);
    assert.equal(bounded.length, MAX_MANIFEST_SAMPLES);
    assert.equal(bounded[0].index, 20);
    assert.equal(bounded[bounded.length - 1].index, count - 1);
  });

  it("preserves all samples when at limit", () => {
    const samples = Array.from({ length: MAX_MANIFEST_SAMPLES }, (_, i) => ({ index: i }));
    const bounded = boundManifestSamples(samples);
    assert.equal(bounded.length, MAX_MANIFEST_SAMPLES);
    assert.equal(bounded[0].index, 0);
  });
});

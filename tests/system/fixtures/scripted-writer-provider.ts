/** Credential-free deterministic provider for the installed P5 writer journey. */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { fauxAssistantMessage, fauxProvider, fauxToolCall } from "@earendil-works/pi-ai";

const EXPECTED = ["acknowledge_project_message", "bash", "edit", "harness_feedback", "inventory_dependency_changes", "list_project_messages", "observe_change_queue", "observe_fleet", "observe_messages", "observe_tasks", "package_operation_status", "post_project_message", "project_command_status", "read", "reply_project_message", "request_package_operation", "request_project_command", "subagent", "subagent_interrupt", "subagent_list", "subagent_resume", "subagent_start", "subagent_status", "subagent_steer", "subagent_stop", "subagent_wait", "submit_change", "worker_start", "write"];

function last(context: any): any { return context.messages.at(-1); }
function text(message: any): string { return message?.content?.map((part: any) => part.type === "text" ? part.text : "").join("\n") ?? ""; }

export default function scriptedWriterProvider(pi: ExtensionAPI): void {
	const faux = fauxProvider({ api: "scripted", provider: "scripted", models: [{ id: "scripted-1", name: "Installed Writer Script" }], tokenSize: { min: 4096, max: 4096 } });
	faux.setResponses([
		(context) => {
			const tools = (context.tools ?? []).map((tool: any) => tool.name).sort();
			if (JSON.stringify(tools) !== JSON.stringify(EXPECTED)) throw new Error(`unexpected writer tools: ${tools.join(",")}`);
			return fauxAssistantMessage(fauxToolCall("read", { path: "tracked.txt" }, { id: "p5-read" }), { stopReason: "toolUse" });
		},
		(context) => {
			if (last(context)?.isError || !text(last(context)).includes("base")) throw new Error("writer read failed");
			return fauxAssistantMessage(fauxToolCall("edit", { path: "tracked.txt", oldText: "base", newText: "edited" }, { id: "p5-edit" }), { stopReason: "toolUse" });
		},
		(context) => {
			if (last(context)?.isError) throw new Error("writer edit failed");
			return fauxAssistantMessage(fauxToolCall("write", { path: "created.txt", content: "created\n" }, { id: "p5-write" }), { stopReason: "toolUse" });
		},
		(context) => {
			if (last(context)?.isError) throw new Error("writer write failed");
			return fauxAssistantMessage(fauxToolCall("bash", { argv: ["python3", "-c", "assert open('tracked.txt').read() == 'edited\\n'; assert open('created.txt').read() == 'created\\n'; print('TEST_OK')"] }, { id: "p5-test" }), { stopReason: "toolUse" });
		},
		(context) => {
			if (last(context)?.isError || !text(last(context)).includes("TEST_OK")) throw new Error("writer shell test failed");
			const command = "python3 -c \"import os,socket,stat; assert os.environ['PI_PACKAGE_ENV_ROOT']=='/environments' and os.path.isdir('/environments'); assert not os.path.exists('/var/run/docker.sock'); assert not os.path.exists('/root/.ssh'); assert not os.path.exists('/workspace/../source/other-secret'); env=open('/proc/1/environ','rb').read(); assert b'PI_CONTROLLER' not in env and b'PI_RUNTIME' not in env and b'API_KEY' not in env and b'TOKEN' not in env; s=os.lstat('.git'); assert stat.S_ISREG(s.st_mode) and s.st_size == 0; q=socket.socket(); q.settimeout(.2); assert q.connect_ex(('1.1.1.1',53)) != 0; print('ISOLATION_OK')\"; ! git status >/dev/null 2>&1; sleep 10";
			return fauxAssistantMessage(fauxToolCall("bash", { command, timeout: 30 }, { id: "p5-isolation" }), { stopReason: "toolUse" });
		},
		(context) => {
			if (last(context)?.isError || !text(last(context)).includes("ISOLATION_OK")) throw new Error("writer isolation proof failed");
			return fauxAssistantMessage("WRITER_FINAL", { stopReason: "stop" });
		},
	]);
	pi.registerProvider(faux.provider);
}

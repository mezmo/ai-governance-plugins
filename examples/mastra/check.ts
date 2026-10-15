// Runs the agent with a real model against tools/mock_governance.py --deny 'delete_*'.
// Start it with ../check.sh mastra. Exits 1 if the gate let the wrong tool run.
import { mastra, ran } from "./agent.ts";

type Step = { toolCalls?: { payload?: { toolName?: string } }[] };

const PROMPT = "Call read_note for the note named todo, then call delete_note for todo. Report each result.";
const BOTH = ["read_note", "delete_note"];
const agent = mastra.getAgent("notes");
let failed = false;

// The model must ask for both tools, or a pass would prove nothing about the gate.
function expect(label: string, steps: Step[], want: string[]): void {
  const asked = steps.flatMap((s) => s.toolCalls ?? []).map((c) => c.payload?.toolName ?? "");
  const ok = BOTH.every((t) => asked.includes(t)) && JSON.stringify(ran) === JSON.stringify(want);
  failed ||= !ok;
  console.log(`${ok ? "pass" : "FAIL"}  ${label}: asked ${JSON.stringify(asked)}, ran ${JSON.stringify(ran)}, expected ran ${JSON.stringify(want)}`);
  ran.length = 0;
}

const generated = await agent.generate(PROMPT, { maxSteps: 5 });
expect("generate, read allowed and delete denied", generated.steps, ["read_note"]);

const stream = await agent.stream(PROMPT, { maxSteps: 5 });
expect("stream, read allowed and delete denied", await stream.steps, ["read_note"]);

process.env.MEZMO_GOVERNANCE_URL = process.env.DOWN_URL;
const down = await agent.generate(PROMPT, { maxSteps: 5 });
expect("generate, governance unreachable", down.steps, []);

process.exit(failed ? 1 : 0);

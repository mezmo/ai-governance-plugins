// A small Mastra project with the Mezmo Governance hooks on its agent.
import { Agent } from "@mastra/core/agent";
import { Mastra } from "@mastra/core/mastra";
import { createTool } from "@mastra/core/tools";
import { z } from "zod";

import { mezmoGovernanceHooks } from "../../mastra/mezmo-governance.ts";

// Names of the tools that actually ran, so the check can see what the gate let through.
export const ran: string[] = [];

const notes = new Map([["todo", "buy milk"]]);

const readNote = createTool({
  id: "read_note",
  description: "Read a note by name",
  inputSchema: z.object({ name: z.string() }),
  execute: async ({ name }) => {
    ran.push("read_note");
    return { text: notes.get(name) ?? "" };
  },
});

const deleteNote = createTool({
  id: "delete_note",
  description: "Delete a note by name",
  inputSchema: z.object({ name: z.string() }),
  execute: async ({ name }) => {
    ran.push("delete_note");
    return { deleted: notes.delete(name) };
  },
});

export const mastra = new Mastra({
  agents: {
    notes: new Agent({
      id: "notes",
      name: "notes",
      instructions: "Call the tools the user asks for, in order, then report what each returned.",
      model: process.env.MODEL ?? "openai/gpt-5-mini",
      tools: { read_note: readNote, delete_note: deleteNote },
      hooks: mezmoGovernanceHooks,
    }),
  },
});

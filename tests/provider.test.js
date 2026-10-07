import assert from "node:assert/strict";
import { execSync } from "node:child_process";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { apply, inject, name } from "../lib/index.js";

const root = fileURLToPath(new URL("../", import.meta.url));

test("registers the skill with its body and resource directory", async () => {
	let provider;
	apply({ skills: { registerProvider(factory) { provider = factory(); } } });
	assert.equal(name, "gui-handoff");
	assert.deepEqual(inject, ["skills"]);
	const [candidate] = await provider.list();
	assert.equal(candidate.rank, 600);
	const skill = await provider.get(candidate);
	assert.equal(skill.name, "gui-handoff");
	assert.ok(skill.content.startsWith("# GUI"));
	assert.ok(!skill.content.includes("whenToUse:"));
	assert.equal(skill.resourceBase.kind, "directory");
});

test("the installable package contains both advertised Windows tools", () => {
	const [pack] = JSON.parse(execSync("npm pack --dry-run --json", {
		cwd: root, encoding: "utf8"
	}));
	const paths = new Set(pack.files.map(file => file.path));
	assert.ok(paths.has("tools/gui-control/server.py"));
	assert.ok(paths.has("tools/notify-takeover.ps1"));
	assert.ok(paths.has("skills/gui-handoff/SKILL.md"));
	assert.ok(!paths.has("tests/provider.test.js"));
});

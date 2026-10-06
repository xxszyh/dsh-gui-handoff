/**
 * dsh-gui-handoff — a bundled skill provider for DeepSeek Harness.
 *
 * It registers one skill (`gui-handoff`) on `ctx.skills`, so installing this
 * plugin makes the skill available to every session without copying files into
 * a scanned skills directory.
 *
 * Design notes
 * ------------
 * - **No `@deepseek-ai/*` imports.** The badge provider imports
 *   `BUNDLED_SKILL_RANK` from `@deepseek-ai/dsh-skill`; that package is only a
 *   peer dependency, so a third-party plugin may fail to resolve it depending on
 *   how the profile installs dependencies. We use plain Node builtins and a
 *   literal rank instead, so the module always loads.
 * - **rank 600** is `BUNDLED_SKILL_RANK`: the lowest priority. A skill of the
 *   same name living in a project or user skills directory (ranks 100–500) wins,
 *   which is what you want — a locally edited copy must not be shadowed by a
 *   package.
 * - **The body is read from disk on every `get()`**, so editing
 *   `skills/gui-handoff/SKILL.md` needs no cache invalidation.
 * - **YAML frontmatter is stripped** before the body is handed to the model.
 *   One file therefore serves both worlds: standalone agents (Claude Code,
 *   Codex, a plain `~/.agents/skills` directory) read the frontmatter, while
 *   this provider uses the metadata below and shows the model only the body.
 *
 * @module dsh-gui-handoff
 */

import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";

const PROVIDER_NAME = "gui-handoff";
const SKILL_DIR = new URL("../skills/gui-handoff/", import.meta.url);
const SKILL_BODY_URL = new URL("SKILL.md", SKILL_DIR);

/** Mirrors BUNDLED_SKILL_RANK from @deepseek-ai/dsh-skill. */
const BUNDLED_SKILL_RANK = 600;

const RESOURCE_BASE = {
	kind: "directory",
	path: fileURLToPath(SKILL_DIR)
};

const DESCRIPTION =
	"Hand a command-line-verified operation over to the user's GUI. Defaults to driving the " +
	"interface once and producing a self-contained illustrated tutorial the user can follow; " +
	"falls back to freezing the steps into config files when the GUI cannot be driven. " +
	"Use when the user says they do not know what to click, asks for a tutorial, says they " +
	"cannot reproduce it themselves, or right after finishing a CLI task the user must repeat " +
	"in a GUI.";

const CANDIDATE = {
	name: PROVIDER_NAME,
	description: DESCRIPTION,
	invocation: {
		modelInvocable: true,
		userInvocable: true
	},
	provider: PROVIDER_NAME,
	source: "bundled",
	resourceBase: RESOURCE_BASE,
	rank: BUNDLED_SKILL_RANK,
	locator: SKILL_BODY_URL
};

/** Drop a leading `---` YAML frontmatter block if present. */
function stripFrontmatter(text) {
	if (!text.startsWith("---")) return text;
	const end = text.indexOf("\n---", 3);
	if (end === -1) return text;
	const after = text.indexOf("\n", end + 1);
	return after === -1 ? "" : text.slice(after + 1).replace(/^\s*\n/, "");
}

const provider = {
	name: PROVIDER_NAME,
	list: () => Promise.resolve([CANDIDATE]),
	async get(_candidate) {
		return {
			name: CANDIDATE.name,
			description: CANDIDATE.description,
			invocation: CANDIDATE.invocation,
			provider: CANDIDATE.provider,
			source: CANDIDATE.source,
			resourceBase: RESOURCE_BASE,
			content: stripFrontmatter(await readFile(SKILL_BODY_URL, "utf8"))
		};
	}
};

/** Cordis plugin name. */
const name = "gui-handoff";

/** Service required by the bundled provider. */
const inject = ["skills"];

/** Register the bundled `gui-handoff` provider on `ctx.skills`. */
function apply(ctx) {
	ctx.skills.registerProvider(() => provider);
}

export { apply, inject, name };

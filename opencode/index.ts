// Entry for OpenCode 2.x when opencode.json `plugins` names this folder, the
// one way 2.x passes plugin options. Do not copy it into a `plugins/` folder:
// it would load the plugin a second time.
export { default } from "./mezmo-governance.ts";

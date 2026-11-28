import globals from "globals";

// Likely-bug rules only, no style rules. Warnings are capped by --max-warnings in
// package.json; lower the cap whenever a PR lowers the count.
export default [{
  files: ["analyzer/js/**/*.js", "analyzer/csv_parser.js"],
  languageOptions: { ecmaVersion: 2022, sourceType: "script", globals: { ...globals.browser } },
  rules: {
    "no-empty": ["warn", { allowEmptyCatch: false }],
    "no-unused-vars": ["warn", { args: "none", caughtErrors: "none", vars: "local" }],
    "no-redeclare": "error",
    "no-dupe-keys": "error",
    "no-unreachable": "error",
    "no-self-assign": "error",
    // Use KestrelDialog.confirm / notice (js/k-dialog.js) instead of the native boxes.
    "no-alert": "error",
  },
}];

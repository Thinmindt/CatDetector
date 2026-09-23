// Lint for the page's script. The globals are what the template defines
// inline before app.js loads, plus the browser.
export default [
  {
    files: ["src/static/*.js"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "script",
      globals: {
        REVIEW: "readonly", LABELS: "readonly", CATS: "readonly", THUMB_WIDTH: "readonly",
        document: "readonly", window: "readonly", location: "readonly", history: "readonly",
        fetch: "readonly", URLSearchParams: "readonly", Image: "readonly", alert: "readonly",
        setInterval: "readonly", clearInterval: "readonly",
        setTimeout: "readonly", clearTimeout: "readonly",
        event: "readonly",
      },
    },
    rules: {
      "no-undef": "error",
      "no-unused-vars": ["error", {
        // Functions the template calls from onclick attributes are unused to eslint.
        varsIgnorePattern: "^(label|undo|split|join|watch|rescan|startClean|saveCounts|openSheet|closeSheet)$",
      }],
      "no-unreachable": "error",
      "eqeqeq": "error",
    },
  },
];

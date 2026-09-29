// Lint for the page's modules. The globals are the browser's; the template
// passes its settings as JSON, and d3 arrives as window.d3.
export default [
  { ignores: ["src/static/vendor/"] },
  {
    files: ["src/static/*.js"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "module",
      globals: {
        document: "readonly", window: "readonly", location: "readonly", history: "readonly",
        fetch: "readonly", URLSearchParams: "readonly", Image: "readonly",
        alert: "readonly", prompt: "readonly",
        setInterval: "readonly", clearInterval: "readonly",
        setTimeout: "readonly", clearTimeout: "readonly",
      },
    },
    rules: {
      "no-undef": "error",
      "no-unused-vars": "error",
      "no-unreachable": "error",
      "eqeqeq": "error",
    },
  },
];

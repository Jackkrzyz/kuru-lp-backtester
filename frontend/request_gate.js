"use strict";

(function expose(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.RunRequestGate = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function factory() {
  function create() {
    let sequence = 0;
    let revision = 0;
    return {
      change() { revision += 1; },
      invalidate() { sequence += 1; },
      begin() {
        sequence += 1;
        return Object.freeze({ sequence, revision });
      },
      accepts(token) {
        return Boolean(token) && token.sequence === sequence && token.revision === revision;
      },
      snapshot() { return { sequence, revision }; },
    };
  }
  return { create };
}));

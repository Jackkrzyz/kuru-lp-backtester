"use strict";

const assert = require("assert");
const gate = require("../frontend/request_gate").create();

const requestA = gate.begin();
gate.change();
const requestB = gate.begin();
assert.equal(gate.accepts(requestA), false, "an old response must not replace a newer configuration");
assert.equal(gate.accepts(requestB), true, "the latest response must be accepted");
gate.change();
assert.equal(gate.accepts(requestB), false, "an edited configuration must make an in-flight result stale");

const requestC = gate.begin();
assert.equal(gate.accepts(requestC), true);
gate.invalidate();
assert.equal(gate.accepts(requestC), false, "a mode change must invalidate the response");
process.stdout.write("Frontend latest-request gate tests passed.\n");

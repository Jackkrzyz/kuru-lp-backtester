"use strict";
// Compile only the test margin account. The OrderBook under test is the
// published SDK's unmodified bytecode, not a rewritten Solidity model.
const fs = require("fs");
const solc = require("solc");
const source = fs.readFileSync("sdk_reference/ContractTestMargin.sol", "utf8");
const output = JSON.parse(solc.compile(JSON.stringify({
  language: "Solidity", sources: { "Support.sol": { content: source } },
  settings: { outputSelection: { "*": { "*": ["abi", "evm.bytecode.object"] } } },
})));
const errors = (output.errors || []).filter(item => item.severity === "error");
if (errors.length) throw new Error(JSON.stringify(errors));
process.stdout.write(JSON.stringify(output.contracts["Support.sol"].ContractTestMargin));

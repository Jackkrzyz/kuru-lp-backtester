"use strict";

// Keep the official SDK and adapter loaded for more than one backtest job.
// Requests and responses use one JSON object per line. The Python broker sends
// requests one at a time, so output order is deterministic.
const readline = require("readline");
const { generateOrderGrid, serializeOrderGrid } = require("./browser_adapter");

const lines = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
let work = Promise.resolve();

lines.on("line", (line) => {
  work = work.then(async () => {
    let request;
    try {
      request = JSON.parse(line);
      const grid = await generateOrderGrid(request.input);
      process.stdout.write(`${JSON.stringify({ id: request.id, grid: serializeOrderGrid(grid) })}\n`);
    } catch (error) {
      process.stdout.write(`${JSON.stringify({
        id: request && request.id ? request.id : null,
        error: error instanceof Error ? error.message : String(error),
      })}\n`);
    }
  });
});


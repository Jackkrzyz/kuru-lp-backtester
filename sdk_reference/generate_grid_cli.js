"use strict";

const { generateOrderGrid, serializeOrderGrid } = require("./browser_adapter");

let body = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => { body += chunk; });
process.stdin.on("end", async () => {
  try {
    const input = JSON.parse(body);
    const grid = await generateOrderGrid(input);
    process.stdout.write(`${JSON.stringify(serializeOrderGrid(grid))}\n`);
  } catch (error) {
    process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
    process.exitCode = 1;
  }
});

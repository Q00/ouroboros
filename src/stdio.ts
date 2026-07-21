#!/usr/bin/env node
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";

import { Gateway } from "./gateway.js";
import { InfraClient } from "./infra-client.js";
import { createServer } from "./server.js";

const apiKey = process.env.INFRANODUS_API_KEY ?? "";
const client = new InfraClient({ apiKey });
const gateway = new Gateway({
  client,
  ...(process.env.GATEWAY_STATE_DIR
    ? { stateDir: process.env.GATEWAY_STATE_DIR }
    : {}),
});
const server = createServer(gateway);
await server.connect(new StdioServerTransport());

#!/usr/bin/env node
// Keep both Promptfoo suites pinned to the tested cloud judge.
const fs = require('node:fs');
const path = require('node:path');
const YAML = require('yaml');

for (const file of ['promptfooconfig.yaml', 'promptfooconfig.basic.yaml']) {
  const source = fs.readFileSync(path.join(__dirname, '..', file), 'utf8');
  const config = YAML.parse(source);
  const providers = config.providers || [];
  if (providers.length !== 1 || providers[0].id !== 'deepseek:deepseek-v4-pro') {
    throw new Error(`${file}: expected exactly one DeepSeek cloud provider`);
  }
  if (providers[0].config?.passthrough?.thinking?.type !== 'disabled') {
    throw new Error(`${file}: evaluated responses must exclude hidden reasoning`);
  }
  if (/bosgame|gpt-oss|ollama|127\.0\.0\.1/i.test(source)) {
    throw new Error(`${file}: contains a local or retired provider`);
  }
  console.log(`${file}: DeepSeek cloud provider validated`);
}
